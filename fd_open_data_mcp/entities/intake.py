"""Bulk entity intake from a daas.db dump.

One-time migration path (design.md D5 / entity-master migration): transfers all
entity master data from the daas.db ``entities`` + ``entity_datasource_links``
tables into the fd-open-data-mcp ``entities`` + ``entity_source_identifiers``
store. After this, daas becomes a thin client of fd-open-data-mcp for entity
identity (resolve via gateway) and the daas-side ``entities`` /
``entity_datasource_links`` tables can be dropped.

Mapping (daas -> fd):
  entities.code / entity_type   -> entities.code / entity_type   (upsert key)
  entities.name (CJK)           -> entities.name_zh
  entities.name (non-CJK)       -> entities.name_en
  entities.ticker / exchange /  -> entities.metadata_json        (merged dict;
    country_code / isin /           structured fields win over daas metadata
    aliases / status / metadata     on key collision)
  entity_datasource_links       -> entity_source_identifiers     (daas source_id
    (entity_id, source_id,            resolved to source name; daas entity_id
     identifier_in_source)            resolved to fd entity id by type+code)

Idempotent: re-running upserts (no duplicates) and refreshes metadata.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from fd_open_data_mcp.entities.resolver import _bulk_upsert_identifiers

# CJK ranges: Hiragana/Katakana, CJK ext-A, CJK unified, fullwidth forms.
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿＀-￯]")


def _is_cjk(s: Optional[str]) -> bool:
    """True if ``s`` contains any CJK character (-> name_zh, else name_en)."""
    return bool(s and _CJK_RE.search(s))


def _read_daas_dump(daas_db_path: str):
    """Read entities + links (+ source-name map) from a daas.db file.

    Returns ``(entities, links)`` where:
      entities = list[dict] (id, entity_type, code, name, ticker, exchange,
                 country_code, isin, aliases, status, metadata)
      links    = list[dict] (daas_entity_id, source_name, identifier)
    """
    conn = sqlite3.connect(daas_db_path)
    conn.row_factory = sqlite3.Row
    try:
        entities = []
        for row in conn.execute(
            "SELECT id, entity_type, code, name, ticker, exchange, "
            "country_code, isin, aliases, status, metadata FROM entities"
        ):
            entities.append({
                "id": row["id"],
                "entity_type": row["entity_type"],
                "code": row["code"],
                "name": row["name"],
                "ticker": row["ticker"],
                "exchange": row["exchange"],
                "country_code": row["country_code"],
                "isin": row["isin"],
                "aliases": json.loads(row["aliases"]) if row["aliases"] else None,
                "status": row["status"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
            })

        # daas source_id -> source name (the fd store keys identifiers by name).
        source_names = {
            r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM sources")
        }

        links = []
        for row in conn.execute(
            "SELECT entity_id, source_id, identifier_in_source "
            "FROM entity_datasource_links"
        ):
            src_name = source_names.get(row["source_id"])
            if not src_name:
                continue
            links.append({
                "daas_entity_id": row["entity_id"],
                "source_name": src_name,
                "identifier": row["identifier_in_source"],
            })
        return entities, links
    finally:
        conn.close()


def _bulk_upsert_entities(
    session: Session, rows: list[dict]
) -> dict[tuple[str, str], int]:
    """Bulk upsert entity rows; return a ``(entity_type, code) -> fd id`` map.

    ``rows``: dicts with entity_type, code, name_en, name_zh, metadata_json (dict).
    Mirrors ``_bulk_upsert_identifiers``: psycopg2 execute_values on Postgres,
    SQLAlchemy executemany on SQLite, both with ON CONFLICT DO UPDATE.
    """
    if not rows:
        return {}
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    raw = session.connection().connection
    is_postgres = type(raw).__module__.startswith("psycopg2")
    sql = """
        INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json, updated_at)
        VALUES %s
        ON CONFLICT (entity_type, code) DO UPDATE SET
            name_en = EXCLUDED.name_en,
            name_zh = EXCLUDED.name_zh,
            metadata_json = EXCLUDED.metadata_json,
            updated_at = EXCLUDED.updated_at
    """
    data = [
        (r["entity_type"], r["code"], r["name_en"], r["name_zh"],
         json.dumps(r["metadata_json"], ensure_ascii=False) if r["metadata_json"] else None, now)
        for r in rows
    ]
    try:
        if is_postgres:
            from psycopg2.extras import execute_values
            cur = raw.cursor()
            try:
                execute_values(cur, sql, data)
            finally:
                cur.close()
        else:
            # SQLite >= 3.24 supports ON CONFLICT ... DO UPDATE ... = EXCLUDED.<col>.
            session.execute(
                text(sql.replace("%s", "(:et, :code, :ne, :nz, :meta, :ts)")),
                [{"et": d[0], "code": d[1], "ne": d[2], "nz": d[3],
                  "meta": d[4], "ts": d[5]} for d in data],
            )
    except ImportError:
        session.execute(
            text(sql.replace("%s", "(:et, :code, :ne, :nz, :meta, :ts)")),
            [{"et": d[0], "code": d[1], "ne": d[2], "nz": d[3],
              "meta": d[4], "ts": d[5]} for d in data],
        )
    session.commit()

    # Build (entity_type, code) -> fd id for everything we just wrote, so the
    # link rows can resolve daas entity_id -> fd entity_id. Batched OR-clauses
    # keep the param count bounded for the 5k+ entity dump.
    keys = [(r["entity_type"], r["code"]) for r in rows]
    id_map: dict[tuple[str, str], int] = {}
    BATCH = 500
    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        params: dict = {}
        clauses = []
        for j, (et, code) in enumerate(chunk):
            params[f"et{j}"] = et
            params[f"code{j}"] = code
            clauses.append(f"(entity_type = :et{j} AND code = :code{j})")
        result = session.execute(
            text(f"SELECT id, entity_type, code FROM entities WHERE {' OR '.join(clauses)}"),
            params,
        )
        for r in result:
            id_map[(r.entity_type, r.code)] = r.id
    return id_map


def ingest_entities_from_dump(
    session: Session, daas_db_path: str, dry_run: bool = False,
) -> dict:
    """Ingest entities + source-identifier links from a daas.db dump.

    Idempotent: re-running upserts (no duplicates) and refreshes metadata.
    ``dry_run`` reads the dump and reports counts without writing.
    """
    entities, links = _read_daas_dump(daas_db_path)

    if dry_run:
        return {
            "dry_run": True,
            "entities": len(entities),
            "links": len(links),
            "entity_types": sorted({e["entity_type"] for e in entities}),
        }

    # 1. Map + bulk-upsert entities.
    ent_rows = []
    for e in entities:
        meta: dict = {}
        if e.get("metadata"):
            meta.update(e["metadata"])  # extra daas metadata first
        for k in ("ticker", "exchange", "country_code", "isin", "aliases", "status"):
            v = e.get(k)
            if v:  # skip empty strings / None / empty lists
                meta[k] = v
        name = e.get("name")
        ent_rows.append({
            "entity_type": e["entity_type"],
            "code": e["code"],
            "name_en": None if _is_cjk(name) else name,
            "name_zh": name if _is_cjk(name) else None,
            "metadata_json": meta or None,
        })
    id_map = _bulk_upsert_entities(session, ent_rows)

    # 2. Resolve daas links -> fd entity_source_identifiers rows.
    daas_id_to_key = {e["id"]: (e["entity_type"], e["code"]) for e in entities}
    id_rows: list[dict] = []
    skipped = 0
    for link in links:
        key = daas_id_to_key.get(link["daas_entity_id"])
        if key is None or key not in id_map:
            skipped += 1
            continue
        if not link["identifier"]:
            continue
        id_rows.append({
            "entity_type": key[0],
            "entity_id": id_map[key],
            "source": link["source_name"],
            "identifier": link["identifier"],
        })
    _bulk_upsert_identifiers(session, id_rows)

    return {
        "entities_upserted": len(ent_rows),
        "identifiers_upserted": len(id_rows),
        "links_skipped": skipped,
        "entity_types": sorted({e["entity_type"] for e in entities}),
    }

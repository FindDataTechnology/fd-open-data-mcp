"""Task 3.1 — bulk entity intake from a daas.db dump.

Verifies the one-time migration path (design.md D5 / entity-master migration):
``ingest_entities_from_dump`` reads a daas.db file's ``entities`` +
``entity_datasource_links`` + ``sources`` tables and bulk-upserts them into the
fd-open-data-mcp ``entities`` + ``entity_source_identifiers`` store, mapping:

  daas name (CJK)  -> name_zh   |   daas name (non-CJK) -> name_en
  daas ticker/exchange/country_code/isin/aliases/status -> metadata_json
  daas links (entity_id, source_id, identifier) -> entity_source_identifiers
      (source_id resolved to source name; daas entity_id resolved to fd id)
"""
import json
import sqlite3

import pytest
from sqlalchemy import text

from fd_open_data_mcp.entities.intake import ingest_entities_from_dump


# --- helpers ----------------------------------------------------------------

def _make_daas_dump(path: str) -> None:
    """Create a minimal daas.db with the 3 source tables + sample rows."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            entity_type VARCHAR(32) NOT NULL,
            code VARCHAR(64) NOT NULL,
            name VARCHAR(255) NOT NULL,
            ticker VARCHAR(64),
            exchange VARCHAR(32),
            country_code VARCHAR(8),
            isin VARCHAR(16),
            aliases JSON,
            status VARCHAR(16) NOT NULL,
            metadata JSON,
            created_at DATETIME,
            updated_at DATETIME,
            UNIQUE (entity_type, code)
        );
        CREATE TABLE entity_datasource_links (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            identifier_in_source VARCHAR(128),
            coverage VARCHAR(16) NOT NULL,
            metadata JSON,
            last_fetched_at DATETIME,
            created_at DATETIME,
            UNIQUE (entity_id, source_id)
        );
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            name VARCHAR(64) NOT NULL,
            label VARCHAR(128) NOT NULL,
            enabled BOOLEAN NOT NULL
        );
    """)
    # daas source id -> name (matches the live daas.db map).
    conn.executemany(
        "INSERT INTO sources (id, name, label, enabled) VALUES (?, ?, ?, 1)",
        [(3, "worldbank", "World Bank"), (22, "yfinance", "YFinance"),
         (2, "cnstats", "CN Stats"), (26, "wbdata", "WB Data")],
    )
    # 1 country (English name -> name_en) + 1 stock (CJK name -> name_zh).
    conn.executemany(
        "INSERT INTO entities (id, entity_type, code, name, ticker, exchange, "
        "country_code, isin, aliases, status, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "country", "CN", "China", None, None, "CN", None, None, "active", None),
            (2, "stock", "000001", "平安银行", "000001", "SZSE", "CN", None,
             json.dumps(["PAB", "Ping An Bank"]), "active",
             json.dumps({"sector": "banking"})),
        ],
    )
    # links: country CN -> worldbank identifier "CN"; stock 000001 -> yfinance "000001.SZ".
    conn.executemany(
        "INSERT INTO entity_datasource_links (id, entity_id, source_id, "
        "identifier_in_source, coverage) VALUES (?, ?, ?, ?, ?)",
        [(1, 1, 3, "CN", "full"), (2, 2, 22, "000001.SZ", "full")],
    )
    conn.commit()
    conn.close()


# --- tests ------------------------------------------------------------------

def test_ingest_entities_basic(session, tmp_path):
    daas_db = tmp_path / "daas.db"
    _make_daas_dump(str(daas_db))

    result = ingest_entities_from_dump(session, str(daas_db))
    assert result["entities_upserted"] == 2
    assert result["identifiers_upserted"] == 2
    assert result["links_skipped"] == 0
    assert result["entity_types"] == ["country", "stock"]

    # Country CN: non-CJK name -> name_en, no name_zh, country_code in metadata.
    cn = session.execute(
        text("SELECT name_en, name_zh, metadata_json FROM entities "
             "WHERE entity_type='country' AND code='CN'")
    ).first()
    assert cn.name_en == "China"
    assert cn.name_zh is None
    assert json.loads(cn.metadata_json)["country_code"] == "CN"

    # Stock 000001: CJK name -> name_zh, structured fields + daas metadata merged.
    stk = session.execute(
        text("SELECT name_en, name_zh, metadata_json FROM entities "
             "WHERE entity_type='stock' AND code='000001'")
    ).first()
    assert stk.name_en is None
    assert stk.name_zh == "平安银行"
    meta = json.loads(stk.metadata_json)
    assert meta["ticker"] == "000001"
    assert meta["exchange"] == "SZSE"
    assert meta["country_code"] == "CN"
    assert meta["aliases"] == ["PAB", "Ping An Bank"]
    assert meta["sector"] == "banking"  # daas metadata preserved
    assert meta["status"] == "active"

    # Identifiers: daas source_id resolved to source name; daas entity_id -> fd id.
    cn_id = session.execute(
        text("SELECT id FROM entities WHERE entity_type='country' AND code='CN'")
    ).first().id
    stk_id = session.execute(
        text("SELECT id FROM entities WHERE entity_type='stock' AND code='000001'")
    ).first().id

    cn_ident = session.execute(
        text("SELECT source, identifier FROM entity_source_identifiers "
             "WHERE entity_type='country' AND entity_id=:eid"),
        {"eid": cn_id},
    ).first()
    assert cn_ident.source == "worldbank"
    assert cn_ident.identifier == "CN"

    stk_ident = session.execute(
        text("SELECT source, identifier FROM entity_source_identifiers "
             "WHERE entity_type='stock' AND entity_id=:eid"),
        {"eid": stk_id},
    ).first()
    assert stk_ident.source == "yfinance"
    assert stk_ident.identifier == "000001.SZ"


def test_ingest_entities_idempotent(session, tmp_path):
    daas_db = tmp_path / "daas.db"
    _make_daas_dump(str(daas_db))

    r1 = ingest_entities_from_dump(session, str(daas_db))
    r2 = ingest_entities_from_dump(session, str(daas_db))
    assert r1 == r2  # same counts on re-run

    n_entities = session.execute(text("SELECT COUNT(*) FROM entities")).scalar()
    n_idents = session.execute(
        text("SELECT COUNT(*) FROM entity_source_identifiers")
    ).scalar()
    assert n_entities == 2
    assert n_idents == 2  # no duplicates


def test_ingest_entities_dry_run(session, tmp_path):
    daas_db = tmp_path / "daas.db"
    _make_daas_dump(str(daas_db))

    result = ingest_entities_from_dump(session, str(daas_db), dry_run=True)
    assert result["dry_run"] is True
    assert result["entities"] == 2
    assert result["links"] == 2
    assert result["entity_types"] == ["country", "stock"]

    # Nothing written.
    assert session.execute(text("SELECT COUNT(*) FROM entities")).scalar() == 0
    assert session.execute(
        text("SELECT COUNT(*) FROM entity_source_identifiers")
    ).scalar() == 0


def test_ingest_entities_skips_unresolved_links(session, tmp_path):
    """A link whose daas entity_id has no matching entity row is skipped."""
    daas_db = tmp_path / "daas.db"
    _make_daas_dump(str(daas_db))
    # Add a dangling link: entity_id=999 does not exist in entities.
    conn = sqlite3.connect(str(daas_db))
    conn.execute(
        "INSERT INTO entity_datasource_links (id, entity_id, source_id, "
        "identifier_in_source, coverage) VALUES (3, 999, 3, 'XX', 'full')"
    )
    conn.commit()
    conn.close()

    result = ingest_entities_from_dump(session, str(daas_db))
    assert result["entities_upserted"] == 2
    assert result["identifiers_upserted"] == 2  # only the 2 resolvable links
    assert result["links_skipped"] == 1

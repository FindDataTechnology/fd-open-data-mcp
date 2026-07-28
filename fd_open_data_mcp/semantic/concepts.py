"""Consume ``indicator_defs`` from fd-entities-indicators into the concepts table.

A concept's canonical identity is ``(code, entity_type, unit, frequency)`` -
two indicator_defs rows sharing ``code`` but differing in ``unit`` or
``frequency`` become two distinct concepts (design.md D2). ``unit`` NULLs are
coerced to "" so the UNIQUE constraint treats them as comparable.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.catalog.providers import finddata_root
from fd_open_data_mcp.models import Concept


def default_entities_db() -> str:
    return str(finddata_root().joinpath("fd-entities-indicators", "entities_indicators.db"))


def consume_indicator_defs(session: Session, db_path: Optional[str] = None) -> dict:
    """Upsert indicator_defs rows into the concepts table. Returns a summary."""
    path = db_path or default_entities_db()
    if not Path(path).exists():
        return {"imported": 0, "total": 0, "errors": [f"entities_indicators.db not found: {path}"]}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT code, name_en, name_zh, category, unit, frequency, source, entity_type "
            "FROM indicator_defs"
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        return {"imported": 0, "total": 0, "errors": [f"read indicator_defs error: {e}"]}
    finally:
        conn.close()

    imported = 0
    for r in rows:
        d = dict(r)
        unit = d.get("unit") or ""
        measure = d.get("measure") or ""
        freq = d.get("frequency") or "unknown"
        etype = d.get("entity_type") or ""
        existing = session.query(Concept).filter_by(
            code=d["code"], entity_type=etype, measure=measure, unit=unit, frequency=freq,
        ).first()
        if existing:
            existing.name_en = d.get("name_en")
            existing.name_zh = d.get("name_zh")
            existing.category = d.get("category")
            existing.source = d.get("source")
            existing.verified = True
        else:
            session.add(Concept(
                code=d["code"], name_en=d.get("name_en"), name_zh=d.get("name_zh"),
                category=d.get("category"), measure=measure, unit=unit, frequency=freq,
                entity_type=etype, source=d.get("source"), verified=True,
            ))
            imported += 1
    session.commit()
    return {"imported": imported, "total": len(rows), "errors": []}

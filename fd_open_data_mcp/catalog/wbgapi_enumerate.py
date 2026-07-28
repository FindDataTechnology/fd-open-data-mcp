"""Enumerate wbgapi (World Bank) indicators into the ontology.

Calls ``wb.series.info()`` (default WDI database, ~1,498 indicators) and, for
each indicator code, upserts a ``columns`` row under ``get_indicator_data``,
a ``concepts`` row (code-named, country/yearly/unknown v1), and a
``concept_bindings`` row. Idempotent via the unique constraints.

Requires the ``data`` extra (wbgapi) + network. On-demand, separate from
``import_catalog``.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import (
    Concept,
    ConceptBinding,
    Function,
    FunctionColumn,
    Source,
)


def _measure_for(code: str) -> str:
    """Derive the statistical measure from a World Bank indicator code."""
    c = (code or "").upper()
    if "MKTP.KD.ZG" in c:
        return "growth"
    if "PCAP.PP" in c:
        return "per_capita_ppp"
    if "PCAP.CD" in c or "PCAP.KD" in c:
        return "per_capita"
    if "MKTP.CD" in c:
        return "nominal_current"
    if "MKTP.KD" in c:
        return "real_constant"
    return ""


def enumerate_wbgapi_indicators(session: Session, db: Optional[str] = None) -> dict:
    """Enumerate WDI indicators into columns + concepts + bindings. Idempotent."""
    db_label = db or "WDI"
    src = session.query(Source).filter_by(name="wbgapi").first()
    if src is None:
        return {"imported": 0, "total": 0, "database": db_label,
                "errors": ["wbgapi source not in catalog; run import_catalog first"]}
    fn = session.query(Function).filter_by(source_id=src.id, command="get_indicator_data").first()
    if fn is None:
        return {"imported": 0, "total": 0, "database": db_label,
                "errors": ["get_indicator_data function not found"]}

    try:
        import wbgapi as wb  # lazy; requires the `data` extra
    except ImportError as e:
        return {"imported": 0, "total": 0, "database": db_label,
                "errors": [f"wbgapi not installed (run 'uv sync --extra data'): {e}"]}

    try:
        fs = wb.series.info(db=db) if db else wb.series.info()
        items = list(getattr(fs, "items", []) or [])
    except Exception as e:  # noqa: BLE001 - network/API failure
        return {"imported": 0, "total": 0, "database": db_label,
                "errors": [f"wb.series.info() failed: {e}"]}

    imported = 0
    for item in items:
        code = item.get("id") if isinstance(item, dict) else None
        if not code:
            continue
        name = (item.get("value") or item.get("name") or code) if isinstance(item, dict) else code

        # upsert column
        col = session.query(FunctionColumn).filter_by(function_id=fn.id, name=code).first()
        if col is None:
            col = FunctionColumn(function_id=fn.id, name=code, type="float", description=name)
            session.add(col)
            session.flush()
        else:
            col.description = name
            col.type = "float"

        # upsert concept (code-named, country/yearly/unknown v1; measure derived from the code)
        measure = _measure_for(code)
        concept = session.query(Concept).filter_by(
            code=code, entity_type="country", measure=measure, unit="unknown", frequency="yearly",
        ).first()
        if concept is None:
            concept = Concept(
                code=code, name_en=name, entity_type="country", measure=measure, unit="unknown",
                frequency="yearly", verified=False,
            )
            session.add(concept)
            session.flush()
        elif not concept.name_en:
            concept.name_en = name

        # upsert binding
        binding = session.query(ConceptBinding).filter_by(
            concept_id=concept.id, column_id=col.id,
        ).first()
        if binding is None:
            session.add(ConceptBinding(
                concept_id=concept.id, column_id=col.id,
                confidence=0.9, provenance="manual", reviewed=True,
            ))
            imported += 1

    session.commit()
    return {"imported": imported, "total": len(items), "database": db_label, "errors": []}

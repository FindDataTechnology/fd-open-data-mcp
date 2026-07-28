"""Register a DatasourceManifest into the ontology (the ingest side of the protocol).

``register_datasource(manifest, session)`` upserts sources/functions/columns/
concepts/concept_bindings from a parsed ``DatasourceManifest`` (from
``fd-open-data-protocol``). Idempotent via the unique constraints.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from fd_open_data_mcp.catalog.enrich import derive_meaning
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, Function, FunctionColumn, Source,
)


def register_datasource(manifest: Any, session: Session) -> dict:
    """Upsert a DatasourceManifest into the ontology. Returns a summary.

    ``manifest`` is a ``fd_open_data_protocol.schema.DatasourceManifest``.
    """
    src = session.query(Source).filter_by(name=manifest.name).first()
    if src is None:
        src = Source(name=manifest.name, label=manifest.label, url=manifest.source_url)
        session.add(src)
        session.flush()
    else:
        src.label = manifest.label
        src.url = manifest.source_url

    fn_count = col_count = 0
    col_by_name: dict[str, FunctionColumn] = {}  # first occurrence per name (for concept hints)

    for fspec in manifest.functions:
        params = [p.model_dump() for p in fspec.parameters]
        fn = session.query(Function).filter_by(source_id=src.id, command=fspec.command).first()
        if fn is None:
            fn = Function(
                source_id=src.id, command=fspec.command, category=fspec.category,
                description=fspec.description, parameters=params, verified=fspec.verified,
                scanner_mode=manifest.scanner_mode, frequency=fspec.frequency,
            )
            session.add(fn)
            session.flush()
            fn_count += 1
        else:
            fn.category = fspec.category
            fn.description = fspec.description
            fn.parameters = params
            fn.verified = fspec.verified
            fn.scanner_mode = manifest.scanner_mode
            fn.frequency = fspec.frequency

        existing_cols = {c.name: c for c in fn.columns}
        seen: set[str] = set()
        for cspec in fspec.columns:
            if cspec.name in seen:
                continue
            seen.add(cspec.name)
            meaning = derive_meaning(cspec.description)
            if cspec.name in existing_cols:
                c = existing_cols[cspec.name]
                c.type = cspec.type
                c.description = cspec.description
                c.meaning = meaning
                c.semantic_type = cspec.semantic_type
                c.frequency = cspec.frequency
                c.datasource = cspec.datasource
            else:
                c = FunctionColumn(
                    function_id=fn.id, name=cspec.name, type=cspec.type,
                    description=cspec.description, meaning=meaning,
                    semantic_type=cspec.semantic_type,
                    frequency=cspec.frequency, datasource=cspec.datasource,
                )
                session.add(c)
                session.flush()
                col_count += 1
            if cspec.name not in col_by_name:
                col_by_name[cspec.name] = c

    # concept hints -> concepts + concept_bindings
    concept_count = binding_count = 0
    for hint in manifest.concepts:
        col = col_by_name.get(hint.column)
        if col is None:
            continue
        measure = hint.measure or ""
        unit = hint.unit or ""
        freq = hint.frequency or "unknown"
        concept = session.query(Concept).filter_by(
            code=hint.concept, entity_type=hint.entity_type,
            measure=measure, unit=unit, frequency=freq,
        ).first()
        if concept is None:
            concept = Concept(
                code=hint.concept, entity_type=hint.entity_type, measure=measure,
                unit=unit, frequency=freq, verified=False,
            )
            session.add(concept)
            session.flush()
            concept_count += 1
        binding = session.query(ConceptBinding).filter_by(
            concept_id=concept.id, column_id=col.id,
        ).first()
        if binding is None:
            session.add(ConceptBinding(
                concept_id=concept.id, column_id=col.id,
                confidence=hint.confidence, provenance="manual", reviewed=True,
            ))
            binding_count += 1

    session.commit()
    return {
        "name": manifest.name, "functions": fn_count, "columns": col_count,
        "concepts": concept_count, "bindings": binding_count,
    }


def discover_datasources(session: Session) -> list[dict]:
    """Auto-discover + register manifests from entry points + a datasources/ dir.

    Entry points: group ``fd_open_data_mcp.datasources`` -> ``"pkg.mod:CATALOG"``.
    Dir: ``FD_OPEN_DATA_DATASOURCES_DIR`` env var pointing at a folder of
    YAML/JSON/Python manifests. Discovered manifests are registered (idempotent).
    """
    import os
    from importlib.metadata import entry_points
    from pathlib import Path

    from fd_open_data_protocol.loader import load_catalog

    results: list[dict] = []

    # entry points
    try:
        eps = entry_points(group="fd_open_data_mcp.datasources")
    except TypeError:  # pragma: no cover - older importlib.metadata
        eps = entry_points().get("fd_open_data_mcp.datasources", [])
    for ep in eps:
        try:
            catalog = ep.load()
            manifest = load_catalog(catalog)
            results.append(register_datasource(manifest, session))
        except Exception as e:  # noqa: BLE001
            results.append({"entry_point": ep.name, "error": str(e)})

    # dir scan
    dir_path = os.environ.get("FD_OPEN_DATA_DATASOURCES_DIR")
    if dir_path:
        d = Path(dir_path)
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.suffix.lower() not in (".yaml", ".yml", ".json", ".py"):
                    continue
                try:
                    manifest = load_catalog(str(f))
                    results.append(register_datasource(manifest, session))
                except Exception as e:  # noqa: BLE001
                    results.append({"file": str(f), "error": str(e)})

    return results

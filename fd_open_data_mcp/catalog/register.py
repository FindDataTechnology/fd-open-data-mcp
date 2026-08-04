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
    Concept, ConceptBinding, Entity, EntityRelationship, Function, FunctionColumn, Source,
)


def upsert_entity(session: Session, entity_spec: Any, source_name: str = None) -> tuple[Entity, str]:
    """Upsert an Entity from protocol schema into the database.

    Args:
        session: SQLAlchemy session
        entity_spec: Entity object from fd_open_data_protocol.schema
        source_name: Optional source name for metadata tracking

    Returns:
        Tuple of (Entity instance, status: "created"|"updated")
    """
    # Find existing entity by (entity_type, code)
    entity = session.query(Entity).filter_by(
        entity_type=entity_spec.entity_type,
        code=entity_spec.code,
    ).first()

    if entity is None:
        # Create new entity
        entity = Entity(
            entity_type=entity_spec.entity_type,
            code=entity_spec.code,
            name_en=entity_spec.name_en,
            name_zh=entity_spec.name_zh,
            metadata_json=entity_spec.metadata or {},
        )
        if source_name:
            entity.metadata_json["source"] = source_name
        session.add(entity)
        session.flush()
        return entity, "created"
    else:
        # Update existing entity
        updated = False
        if entity_spec.name_en and entity.name_en != entity_spec.name_en:
            entity.name_en = entity_spec.name_en
            updated = True
        if entity_spec.name_zh and entity.name_zh != entity_spec.name_zh:
            entity.name_zh = entity_spec.name_zh
            updated = True

        # Merge metadata
        if entity_spec.metadata:
            current_metadata = entity.metadata_json or {}
            current_metadata.update(entity_spec.metadata)
            if source_name:
                current_metadata["source"] = source_name
            entity.metadata_json = current_metadata
            updated = True

        return entity, "updated" if updated else "unchanged"


def upsert_entity_relationship(
    session: Session,
    source_entity: Entity,
    relationship_spec: Any,
) -> tuple[EntityRelationship, str]:
    """Upsert an EntityRelationship from protocol schema into the database.

    Args:
        session: SQLAlchemy session
        source_entity: Source Entity instance
        relationship_spec: EntityRelationship object from fd_open_data_protocol.schema

    Returns:
        Tuple of (EntityRelationship instance, status: "created"|"updated"|"skipped")
    """
    # Find target entity
    target_entity = session.query(Entity).filter_by(
        entity_type=relationship_spec.target_entity_type,
        code=relationship_spec.target_code,
    ).first()

    if target_entity is None:
        # Target entity doesn't exist yet - skip this relationship
        # (it will be created when the target entity is registered)
        return None, "skipped"

    # Check if relationship already exists
    rel = session.query(EntityRelationship).filter_by(
        source_id=source_entity.id,
        relation_type=relationship_spec.relation_type,
        target_id=target_entity.id,
    ).first()

    if rel is None:
        # Create new relationship
        rel = EntityRelationship(
            source_id=source_entity.id,
            relation_type=relationship_spec.relation_type,
            target_id=target_entity.id,
            metadata_json=relationship_spec.metadata or {},
        )
        session.add(rel)
        session.flush()
        return rel, "created"
    else:
        # Update metadata if provided
        if relationship_spec.metadata:
            current_metadata = rel.metadata_json or {}
            current_metadata.update(relationship_spec.metadata)
            rel.metadata_json = current_metadata
            return rel, "updated"
        return rel, "unchanged"


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

    # entities -> entities table (coverage declarations)
    entity_count = 0
    for espec in manifest.entities:
        # For explicit coverage, create individual entity records
        # For universe coverage, we just note that this source covers this entity type
        if espec.coverage == "explicit" and espec.codes:
            for code in espec.codes:
                entity = session.query(Entity).filter_by(
                    entity_type=espec.entity_type, code=code,
                ).first()
                if entity is None:
                    entity = Entity(
                        entity_type=espec.entity_type, code=code,
                        metadata_json={"source": manifest.name, "coverage": "explicit"},
                    )
                    session.add(entity)
                    session.flush()
                    entity_count += 1
                else:
                    # Update metadata to note this source covers it
                    metadata = entity.metadata_json or {}
                    metadata["source"] = manifest.name
                    entity.metadata_json = metadata

    # entity_definitions -> entities table (canonical entity metadata)
    entity_def_count = 0
    relationship_count = 0
    entity_map: dict[tuple[str, str], Entity] = {}  # (entity_type, code) -> Entity

    if hasattr(manifest, 'entity_definitions') and manifest.entity_definitions:
        # First pass: create/update all entities
        for entity_spec in manifest.entity_definitions:
            entity, status = upsert_entity(session, entity_spec, manifest.name)
            entity_map[(entity_spec.entity_type, entity_spec.code)] = entity
            if status == "created":
                entity_def_count += 1

        # Second pass: create relationships (after all entities exist)
        for entity_spec in manifest.entity_definitions:
            if entity_spec.relationships:
                source_entity = entity_map.get((entity_spec.entity_type, entity_spec.code))
                if source_entity:
                    for rel_spec in entity_spec.relationships:
                        rel, rel_status = upsert_entity_relationship(session, source_entity, rel_spec)
                        if rel_status == "created":
                            relationship_count += 1

    # relationships -> store resolver info in source metadata
    # We don't create relationship records yet - we just note that this source
    # can resolve these relationships via the resolver_module
    if manifest.relationships:
        source_metadata = src.metadata_json or {} if hasattr(src, 'metadata_json') else {}
        source_metadata["relationship_resolvers"] = [
            {
                "relation_type": rspec.relation_type,
                "source_entity_type": rspec.source_entity_type,
                "target_entity_type": rspec.target_entity_type,
                "resolver_module": rspec.resolver_module,
            }
            for rspec in manifest.relationships
        ]
        if hasattr(src, 'metadata_json'):
            src.metadata_json = source_metadata

    session.commit()
    return {
        "name": manifest.name, "functions": fn_count, "columns": col_count,
        "concepts": concept_count, "bindings": binding_count,
        "entities": entity_count + entity_def_count,
        "entity_definitions": entity_def_count,
        "relationships": relationship_count,
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

"""MCP tools for entity graph operations.

Provides tools for managing entities and their relationships in the unified
entity registry.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp.server import mcp


@mcp.tool()
def list_entities(
    entity_type: str,
    limit: int = 100,
    offset: int = 0
) -> list[dict]:
    """List entities of a specific type.

    Args:
        entity_type: Type of entity (country, city, company, stock, etf, bond, index, future, crypto)
        limit: Maximum number of entities to return
        offset: Number of entities to skip

    Returns:
        List of entity dictionaries
    """
    db = dbmod.get_database()
    session = db.get_session()

    try:
        result = session.execute(
            text("""
                SELECT id, entity_type, code, name_en, name_zh, metadata_json
                FROM entities
                WHERE entity_type = :entity_type
                ORDER BY code
                LIMIT :limit OFFSET :offset
            """),
            {"entity_type": entity_type, "limit": limit, "offset": offset}
        )

        entities = []
        for row in result:
            # Handle both dict (psycopg2 JSONB) and string (SQLite JSON) formats
            metadata = row.metadata_json
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            entity = {
                "id": row.id,
                "entity_type": row.entity_type,
                "code": row.code,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "metadata": metadata,
            }
            entities.append(entity)

        return entities

    finally:
        session.close()


@mcp.tool()
def get_entity(
    entity_type: str,
    code: str
) -> Optional[dict]:
    """Get a specific entity by type and code.

    Args:
        entity_type: Type of entity (country, city, company, stock, etc.)
        code: Entity code (e.g., 'CN' for China, 'AAPL' for Apple stock)

    Returns:
        Entity dictionary or None if not found
    """
    db = dbmod.get_database()
    session = db.get_session()

    try:
        result = session.execute(
            text("""
                SELECT id, entity_type, code, name_en, name_zh, metadata_json
                FROM entities
                WHERE entity_type = :entity_type AND code = :code
            """),
            {"entity_type": entity_type, "code": code}
        ).first()

        if not result:
            return None

        # Handle both dict (psycopg2 JSONB) and string (SQLite JSON) formats
        metadata = result.metadata_json
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        return {
            "id": result.id,
            "entity_type": result.entity_type,
            "code": result.code,
            "name_en": result.name_en,
            "name_zh": result.name_zh,
            "metadata": metadata,
        }

    finally:
        session.close()


@mcp.tool()
def add_entity(
    entity_type: str,
    code: str,
    name_en: Optional[str] = None,
    name_zh: Optional[str] = None,
    metadata: Optional[dict] = None
) -> dict:
    """Add a new entity to the registry.

    Args:
        entity_type: Type of entity (country, city, company, stock, etc.)
        code: Entity code (unique within entity_type)
        name_en: English name
        name_zh: Chinese name
        metadata: Additional metadata as JSON object

    Returns:
        Created entity dictionary
    """
    db = dbmod.get_database()
    session = db.get_session()

    try:
        from fd_open_data_mcp.entities.taxonomy import validate_entity_type
        validate_entity_type(entity_type)

        # Check if already exists
        existing = session.execute(
            text("SELECT id FROM entities WHERE entity_type = :entity_type AND code = :code"),
            {"entity_type": entity_type, "code": code}
        ).first()

        if existing:
            raise ValueError(f"Entity {entity_type}:{code} already exists")

        # Insert new entity
        metadata_json = json.dumps(metadata) if metadata else None

        result = session.execute(
            text("""
                INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json)
                VALUES (:entity_type, :code, :name_en, :name_zh, :metadata)
                RETURNING id
            """),
            {
                "entity_type": entity_type,
                "code": code,
                "name_en": name_en,
                "name_zh": name_zh,
                "metadata": metadata_json,
            }
        ).first()

        session.commit()

        return {
            "id": result.id,
            "entity_type": entity_type,
            "code": code,
            "name_en": name_en,
            "name_zh": name_zh,
            "metadata": metadata,
        }

    except Exception as e:
        session.rollback()
        raise
    finally:
        session.close()


@mcp.tool()
def list_relationships(
    entity_type: str,
    code: str,
    direction: str = "outgoing"
) -> list[dict]:
    """List relationships for an entity.

    Args:
        entity_type: Type of entity
        code: Entity code
        direction: 'outgoing' (entity is source) or 'incoming' (entity is target)

    Returns:
        List of relationship dictionaries
    """
    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Get entity ID
        entity = session.execute(
            text("SELECT id FROM entities WHERE entity_type = :entity_type AND code = :code"),
            {"entity_type": entity_type, "code": code}
        ).first()

        if not entity:
            return []

        entity_id = entity.id

        if direction == "outgoing":
            # Entity is source
            result = session.execute(
                text("""
                    SELECT r.id, r.relation_type, r.valid_from, r.valid_to, r.metadata_json,
                           e2.entity_type as target_type, e2.code as target_code,
                           e2.name_en as target_name_en, e2.name_zh as target_name_zh
                    FROM entity_relationships r
                    JOIN entities e2 ON r.target_id = e2.id
                    WHERE r.source_id = :entity_id
                    ORDER BY r.relation_type, e2.code
                """),
                {"entity_id": entity_id}
            )
        else:
            # Entity is target
            result = session.execute(
                text("""
                    SELECT r.id, r.relation_type, r.valid_from, r.valid_to, r.metadata_json,
                           e1.entity_type as source_type, e1.code as source_code,
                           e1.name_en as source_name_en, e1.name_zh as source_name_zh
                    FROM entity_relationships r
                    JOIN entities e1 ON r.source_id = e1.id
                    WHERE r.target_id = :entity_id
                    ORDER BY r.relation_type, e1.code
                """),
                {"entity_id": entity_id}
            )

        relationships = []
        for row in result:
            # Handle both dict (psycopg2 JSONB) and string (SQLite JSON) formats
            metadata = row.metadata_json
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            rel = {
                "id": row.id,
                "relation_type": row.relation_type,
                "valid_from": row.valid_from.isoformat() if row.valid_from else None,
                "valid_to": row.valid_to.isoformat() if row.valid_to else None,
                "metadata": metadata,
            }

            if direction == "outgoing":
                rel["target"] = {
                    "entity_type": row.target_type,
                    "code": row.target_code,
                    "name_en": row.target_name_en,
                    "name_zh": row.target_name_zh,
                }
            else:
                rel["source"] = {
                    "entity_type": row.source_type,
                    "code": row.source_code,
                    "name_en": row.source_name_en,
                    "name_zh": row.source_name_zh,
                }

            relationships.append(rel)

        return relationships

    finally:
        session.close()


@mcp.tool()
def add_relationship(
    source_entity_type: str,
    source_code: str,
    relation_type: str,
    target_entity_type: str,
    target_code: str,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
    metadata: Optional[dict] = None
) -> dict:
    """Add a new relationship between two entities.

    Args:
        source_entity_type: Source entity type
        source_code: Source entity code
        relation_type: Type of relationship (e.g., 'located_in', 'operates_in', 'member_of')
        target_entity_type: Target entity type
        target_code: Target entity code
        valid_from: Start date (ISO format, e.g., '2024-01-01')
        valid_to: End date (ISO format, or None for ongoing)
        metadata: Additional metadata as JSON object

    Returns:
        Created relationship dictionary
    """
    from datetime import datetime

    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Get source entity ID
        source = session.execute(
            text("SELECT id FROM entities WHERE entity_type = :entity_type AND code = :code"),
            {"entity_type": source_entity_type, "code": source_code}
        ).first()

        if not source:
            raise ValueError(f"Source entity {source_entity_type}:{source_code} not found")

        # Get target entity ID
        target = session.execute(
            text("SELECT id FROM entities WHERE entity_type = :entity_type AND code = :code"),
            {"entity_type": target_entity_type, "code": target_code}
        ).first()

        if not target:
            raise ValueError(f"Target entity {target_entity_type}:{target_code} not found")

        # Parse dates
        valid_from_dt = datetime.fromisoformat(valid_from) if valid_from else None
        valid_to_dt = datetime.fromisoformat(valid_to) if valid_to else None

        # Check if relationship already exists
        existing = session.execute(
            text("""
                SELECT id FROM entity_relationships
                WHERE source_id = :source_id AND relation_type = :relation_type
                  AND target_id = :target_id AND valid_from IS NOT DISTINCT FROM :valid_from
            """),
            {
                "source_id": source.id,
                "relation_type": relation_type,
                "target_id": target.id,
                "valid_from": valid_from_dt,
            }
        ).first()

        if existing:
            raise ValueError(f"Relationship already exists")

        # Insert new relationship
        metadata_json = json.dumps(metadata) if metadata else None

        result = session.execute(
            text("""
                INSERT INTO entity_relationships (source_id, relation_type, target_id, valid_from, valid_to, metadata_json)
                VALUES (:source_id, :relation_type, :target_id, :valid_from, :valid_to, :metadata)
                RETURNING id
            """),
            {
                "source_id": source.id,
                "relation_type": relation_type,
                "target_id": target.id,
                "valid_from": valid_from_dt,
                "valid_to": valid_to_dt,
                "metadata": metadata_json,
            }
        ).first()

        session.commit()

        return {
            "id": result.id,
            "source": {"entity_type": source_entity_type, "code": source_code},
            "relation_type": relation_type,
            "target": {"entity_type": target_entity_type, "code": target_code},
            "valid_from": valid_from,
            "valid_to": valid_to,
            "metadata": metadata,
        }

    except Exception as e:
        session.rollback()
        raise
    finally:
        session.close()

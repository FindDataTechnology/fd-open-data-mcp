"""
MCP tools for entity sync operations.
Provides query, action, and configuration tools.
"""
import os
from datetime import datetime
from typing import Optional
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def get_database_url() -> str:
    """Get database URL from environment."""
    return os.environ.get(
        "FD_OPEN_DATA_MCP_DATABASE_URL",
        "postgresql://admin:admin123@192.168.1.4:5433/postgres"
    )


# ============================================================================
# Query Tools
# ============================================================================

def list_entity_sources() -> list[dict]:
    """
    List all configured entity sources with their status.

    Returns:
        List of entity source configurations
    """
    engine = create_engine(get_database_url())
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        result = session.execute(text("""
            SELECT
                entity_type,
                source_table,
                source_schema,
                code_column,
                name_en_column,
                name_zh_column,
                select_filter,
                metadata_columns,
                enabled,
                last_sync_at,
                updated_at
            FROM entity_sources
            ORDER BY entity_type
        """))

        sources = []
        for row in result:
            sources.append({
                "entity_type": row.entity_type,
                "source_table": row.source_table,
                "source_schema": row.source_schema,
                "code_column": row.code_column,
                "name_en_column": row.name_en_column,
                "name_zh_column": row.name_zh_column,
                "select_filter": row.select_filter,
                "metadata_columns": row.metadata_columns,
                "enabled": row.enabled,
                "last_sync_at": row.last_sync_at.isoformat() if row.last_sync_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None
            })

        return sources

    finally:
        session.close()


def get_source_config(entity_type: str) -> Optional[dict]:
    """
    Get detailed configuration for a specific entity source.

    Args:
        entity_type: Entity type to query

    Returns:
        Source configuration dict or None if not found
    """
    engine = create_engine(get_database_url())
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        result = session.execute(text("""
            SELECT
                entity_type,
                source_table,
                source_schema,
                code_column,
                name_en_column,
                name_zh_column,
                select_filter,
                metadata_columns,
                enabled,
                last_sync_at,
                updated_at
            FROM entity_sources
            WHERE entity_type = :entity_type
        """), {"entity_type": entity_type})

        row = result.first()
        if not row:
            return None

        return {
            "entity_type": row.entity_type,
            "source_table": row.source_table,
            "source_schema": row.source_schema,
            "code_column": row.code_column,
            "name_en_column": row.name_en_column,
            "name_zh_column": row.name_zh_column,
            "select_filter": row.select_filter,
            "metadata_columns": row.metadata_columns,
            "enabled": row.enabled,
            "last_sync_at": row.last_sync_at.isoformat() if row.last_sync_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None
        }

    finally:
        session.close()


def get_sync_history(entity_type: Optional[str] = None, limit: int = 20) -> list[dict]:
    """
    Get recent sync history logs.

    Args:
        entity_type: Filter by entity type (optional)
        limit: Maximum number of logs to return (default: 20)

    Returns:
        List of sync log dicts
    """
    engine = create_engine(get_database_url())
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        if entity_type:
            result = session.execute(text("""
                SELECT
                    id,
                    entity_type,
                    started_at,
                    finished_at,
                    inserted_count,
                    updated_count,
                    deleted_count,
                    error_count,
                    status,
                    error_message,
                    duration_seconds,
                    scheduler_id
                FROM entity_sync_logs
                WHERE entity_type = :entity_type
                ORDER BY started_at DESC
                LIMIT :limit
            """), {"entity_type": entity_type, "limit": limit})
        else:
            result = session.execute(text("""
                SELECT
                    id,
                    entity_type,
                    started_at,
                    finished_at,
                    inserted_count,
                    updated_count,
                    deleted_count,
                    error_count,
                    status,
                    error_message,
                    duration_seconds,
                    scheduler_id
                FROM entity_sync_logs
                ORDER BY started_at DESC
                LIMIT :limit
            """), {"limit": limit})

        logs = []
        for row in result:
            logs.append({
                "id": row.id,
                "entity_type": row.entity_type,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "inserted_count": row.inserted_count,
                "updated_count": row.updated_count,
                "deleted_count": row.deleted_count,
                "error_count": row.error_count,
                "status": row.status,
                "error_message": row.error_message,
                "duration_seconds": row.duration_seconds,
                "scheduler_id": row.scheduler_id
            })

        return logs

    finally:
        session.close()


# ============================================================================
# Action Tools
# ============================================================================

def trigger_sync(entity_type: Optional[str] = None) -> dict:
    """
    Manually trigger sync for specific or all entity types.

    Args:
        entity_type: Entity type to sync (optional, syncs all if None)

    Returns:
        Sync result dict
    """
    from fd_open_data_mcp.sync.engine import EntitySyncEngine

    engine = EntitySyncEngine(database_url=get_database_url())

    if entity_type:
        # Sync single type
        try:
            return engine.sync_entity_type(entity_type)
        except ValueError as e:
            return {
                "status": "error",
                "error_message": str(e),
                "entity_type": entity_type,
                "inserted_count": 0,
                "updated_count": 0,
                "error_count": 1,
                "duration_seconds": 0
            }
    else:
        # Sync all enabled types
        db_engine = create_engine(get_database_url())
        Session = sessionmaker(bind=db_engine)
        session = Session()

        try:
            result = session.execute(text("""
                SELECT entity_type FROM entity_sources WHERE enabled = TRUE
            """))

            entity_types = [row.entity_type for row in result]
            results = {}

            for et in entity_types:
                try:
                    results[et] = engine.sync_entity_type(et)
                except ValueError as e:
                    # Log error but continue with other types
                    results[et] = {
                        "status": "error",
                        "error_message": str(e),
                        "entity_type": et,
                        "inserted_count": 0,
                        "updated_count": 0,
                        "error_count": 1,
                        "duration_seconds": 0
                    }

            # Determine overall status
            errors = sum(1 for r in results.values() if r['status'] == 'error')
            overall_status = 'partial' if errors > 0 and len(results) - errors > 0 else ('failed' if errors == len(results) else 'success')

            return {
                "status": overall_status,
                "synced_types": len(entity_types),
                "errors": errors,
                "results": results
            }

        finally:
            session.close()


def disable_sync(entity_type: str) -> dict:
    """
    Disable auto-sync for a specific entity type.

    Args:
        entity_type: Entity type to disable

    Returns:
        Result dict
    """
    engine = create_engine(get_database_url())
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        result = session.execute(text("""
            UPDATE entity_sources
            SET enabled = FALSE,
                updated_at = NOW()
            WHERE entity_type = :entity_type
        """), {"entity_type": entity_type})

        session.commit()

        if result.rowcount == 0:
            return {"status": "not_found", "entity_type": entity_type}

        return {"status": "disabled", "entity_type": entity_type}

    except Exception as e:
        session.rollback()
        return {"status": "error", "error_message": str(e)}
    finally:
        session.close()


def enable_sync(entity_type: str) -> dict:
    """
    Enable auto-sync for a specific entity type.

    Args:
        entity_type: Entity type to enable

    Returns:
        Result dict
    """
    engine = create_engine(get_database_url())
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        result = session.execute(text("""
            UPDATE entity_sources
            SET enabled = TRUE,
                updated_at = NOW()
            WHERE entity_type = :entity_type
        """), {"entity_type": entity_type})

        session.commit()

        if result.rowcount == 0:
            return {"status": "not_found", "entity_type": entity_type}

        return {"status": "enabled", "entity_type": entity_type}

    except Exception as e:
        session.rollback()
        return {"status": "error", "error_message": str(e)}
    finally:
        session.close()


def resync_from_date(entity_type: str, date: str) -> dict:
    """
    Force resync from a specific date.

    Args:
        entity_type: Entity type to resync
        date: Start date for resync (ISO format: YYYY-MM-DD)

    Returns:
        Sync result dict
    """
    from fd_open_data_mcp.sync.engine import EntitySyncEngine

    engine = create_engine(get_database_url())
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Update last_sync_at to force resync
        resync_date = datetime.fromisoformat(date)
        session.execute(text("""
            UPDATE entity_sources
            SET last_sync_at = :last_sync_at
            WHERE entity_type = :entity_type
        """), {"entity_type": entity_type, "last_sync_at": resync_date})

        session.commit()

        # Run sync
        sync_engine = EntitySyncEngine(database_url=get_database_url())
        result = sync_engine.sync_entity_type(entity_type)

        return result

    except Exception as e:
        session.rollback()
        return {
            "status": "failed",
            "error_message": str(e),
            "inserted_count": 0,
            "updated_count": 0,
            "error_count": 1,
            "duration_seconds": 0
        }
    finally:
        session.close()


# ============================================================================
# Configuration Tools
# ============================================================================

def update_source_config(entity_type: str, updates: dict) -> dict:
    """
    Update source configuration for an entity type.

    Args:
        entity_type: Entity type to update
        updates: Dict of fields to update

    Returns:
        Result dict
    """
    engine = create_engine(get_database_url())
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Build dynamic UPDATE statement
        set_clauses = []
        params = {"entity_type": entity_type}

        allowed_fields = [
            "source_table", "source_schema", "code_column",
            "name_en_column", "name_zh_column", "select_filter",
            "metadata_columns", "enabled"
        ]

        for field, value in updates.items():
            if field in allowed_fields:
                set_clauses.append(f"{field} = :{field}")
                params[field] = value

        if not set_clauses:
            return {"status": "error", "error_message": "No valid fields to update"}

        set_clauses.append("updated_at = NOW()")
        set_clause = ", ".join(set_clauses)

        result = session.execute(text(f"""
            UPDATE entity_sources
            SET {set_clause}
            WHERE entity_type = :entity_type
        """), params)

        session.commit()

        if result.rowcount == 0:
            return {"status": "not_found", "entity_type": entity_type}

        return {"status": "updated", "entity_type": entity_type, "updated_fields": list(updates.keys())}

    except Exception as e:
        session.rollback()
        return {"status": "error", "error_message": str(e)}
    finally:
        session.close()


def create_custom_source(name: str, config: dict) -> dict:
    """
    Create a new custom entity source.

    Args:
        name: Entity type name
        config: Configuration dict with source_table, code_column, etc.

    Returns:
        Result dict
    """
    engine = create_engine(get_database_url())
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        session.execute(text("""
            INSERT INTO entity_sources (
                entity_type,
                source_table,
                source_schema,
                code_column,
                name_en_column,
                name_zh_column,
                select_filter,
                metadata_columns,
                enabled
            ) VALUES (
                :entity_type,
                :source_table,
                :source_schema,
                :code_column,
                :name_en_column,
                :name_zh_column,
                :select_filter,
                :metadata_columns,
                :enabled
            )
        """), {
            "entity_type": name,
            "source_table": config.get("source_table"),
            "source_schema": config.get("source_schema", "public"),
            "code_column": config.get("code_column"),
            "name_en_column": config.get("name_en_column"),
            "name_zh_column": config.get("name_zh_column"),
            "select_filter": config.get("select_filter"),
            "metadata_columns": config.get("metadata_columns", []),
            "enabled": config.get("enabled", True)
        })

        session.commit()

        return {"status": "created", "entity_type": name}

    except Exception as e:
        session.rollback()
        return {"status": "error", "error_message": str(e)}
    finally:
        session.close()

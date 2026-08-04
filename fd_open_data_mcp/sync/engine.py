"""
Core sync engine for entity data synchronization.
Implements incremental sync with timestamp-based and ID-list fallback detection.
Supports both PostgreSQL and SQLite databases through adapter pattern.
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

from fd_open_data_mcp.db.adapters import get_adapter
from fd_open_data_mcp.sync.locks import get_lock_manager

logger = logging.getLogger(__name__)


class EntitySyncEngine:
    """
    Synchronizes entity data from source tables to entities table.
    Supports incremental sync with timestamp-based detection and ID-list fallback.
    Works with both PostgreSQL and SQLite databases.
    """

    def __init__(self, database_url: str):
        """Initialize sync engine with database connection.

        Args:
            database_url: Database connection string (PostgreSQL or SQLite)
        """
        self.database_url = database_url
        self.engine = create_engine(database_url)
        self.Session = sessionmaker(bind=self.engine)

        # Initialize database adapter
        self.adapter = get_adapter(database_url)

        # Initialize lock manager
        self.lock_manager = get_lock_manager(database_url)

        logger.info(f"Initialized sync engine with {self.adapter.__class__.__name__}")

    def get_source_config(self, entity_type: str) -> Optional[Dict[str, Any]]:
        """
        Task 2.1.1: Get entity source configuration for a given entity_type.

        Args:
            entity_type: Type of entity (e.g., 'stock', 'company')

        Returns:
            Dict with source configuration or None if not found
        """
        session = self.Session()
        try:
            result = session.execute(text("""
                SELECT
                    entity_type, source_table, source_schema,
                    code_column, name_en_column, name_zh_column,
                    select_filter, metadata_columns, enabled, last_sync_at
                FROM entity_sources
                WHERE entity_type = :entity_type
            """), {"entity_type": entity_type}).fetchone()

            if not result:
                return None

            return {
                "entity_type": result.entity_type,
                "source_table": result.source_table,
                "source_schema": result.source_schema,
                "code_column": result.code_column,
                "name_en_column": result.name_en_column,
                "name_zh_column": result.name_zh_column,
                "select_filter": result.select_filter,
                "metadata_columns": result.metadata_columns or [],
                "enabled": result.enabled,
                "last_sync_at": result.last_sync_at
            }
        finally:
            session.close()

    def check_source_has_updated_at(self, source_table: str, source_schema: str = 'public') -> bool:
        """
        Check if source table has updated_at column for timestamp-based sync.

        Args:
            source_table: Name of source table
            source_schema: Schema name (default: 'public')

        Returns:
            True if updated_at column exists, False otherwise
        """
        session = self.Session()
        try:
            result = session.execute(text("""
                SELECT COUNT(*) as count
                FROM information_schema.columns
                WHERE table_schema = :schema
                  AND table_name = :table
                  AND column_name = 'updated_at'
            """), {"schema": source_schema, "table": source_table}).fetchone()

            return result.count > 0
        finally:
            session.close()

    def fetch_entities_incremental(
        self,
        source_config: Dict[str, Any],
        last_sync_at: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Task 2.1.2: Fetch entities using timestamp-based incremental detection.

        Args:
            source_config: Source configuration dict
            last_sync_at: Last sync timestamp (None for full sync)

        Returns:
            List of entity dicts from source table
        """
        session = self.Session()
        try:
            # Build SELECT query
            columns = [
                source_config["code_column"],
                source_config.get("name_en_column", "NULL"),
                source_config.get("name_zh_column", "NULL"),
            ]

            # Add metadata columns - use adapter-specific JSON construction
            metadata_cols = source_config.get("metadata_columns", [])
            if metadata_cols:
                # Use adapter to build JSON object
                metadata_json = self.adapter.build_json_object(metadata_cols)
                columns.append(metadata_json)
            else:
                columns.append("NULL")

            select_clause = ", ".join(columns)

            # Build WHERE clause
            where_clauses = []
            if source_config.get("select_filter"):
                where_clauses.append(f"({source_config['select_filter']})")

            if last_sync_at and self.check_source_has_updated_at(
                source_config["source_table"],
                source_config.get("source_schema", "public")
            ):
                where_clauses.append("updated_at >= :last_sync_at")

            where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"

            query = f"""
                SELECT {select_clause}
                FROM {source_config.get('source_schema', 'public')}.{source_config['source_table']}
                WHERE {where_clause}
            """

            params = {}
            if last_sync_at:
                params["last_sync_at"] = last_sync_at

            results = session.execute(text(query), params).fetchall()

            entities = []
            for row in results:
                # Use adapter to deserialize JSON
                metadata_raw = row[-1] if metadata_cols else None
                metadata_json = self.adapter.deserialize_json(metadata_raw)

                entity = {
                    "code": getattr(row, source_config["code_column"]),
                    "name_en": getattr(row, source_config.get("name_en_column", "name_en"), None) if source_config.get("name_en_column") else None,
                    "name_zh": getattr(row, source_config.get("name_zh_column", "name_zh"), None) if source_config.get("name_zh_column") else None,
                    "metadata_json": metadata_json
                }
                entities.append(entity)

            return entities
        finally:
            session.close()

    def fetch_entities_by_id_list(
        self,
        source_config: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Task 2.1.3: Fetch entities using ID-list comparison (fallback for tables without updated_at).

        Args:
            source_config: Source configuration dict

        Returns:
            List of entity dicts from source table
        """
        session = self.Session()
        try:
            # Build SELECT query
            columns = [
                source_config["code_column"],
                source_config.get("name_en_column", "NULL"),
                source_config.get("name_zh_column", "NULL"),
            ]

            # Add metadata columns as JSON (adapter handles serialization)
            metadata_cols = source_config.get("metadata_columns", [])
            if metadata_cols:
                # Use adapter-specific JSON building
                if self.adapter.__class__.__name__ == "PostgreSQLAdapter":
                    metadata_json = "jsonb_build_object(" + ", ".join(
                        f"'{col}', {col}" for col in metadata_cols
                    ) + ")"
                else:
                    # SQLite: build JSON string in Python after fetch
                    metadata_json = ", ".join(metadata_cols)
                columns.append(metadata_json)
            else:
                columns.append("NULL")

            select_clause = ", ".join(columns)

            # Build WHERE clause
            where_clauses = []
            if source_config.get("select_filter"):
                where_clauses.append(f"({source_config['select_filter']})")

            where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"

            query = f"""
                SELECT {select_clause}
                FROM {source_config.get('source_schema', 'public')}.{source_config['source_table']}
                WHERE {where_clause}
            """

            results = session.execute(text(query)).fetchall()

            entities = []
            for row in results:
                entity = {
                    "code": getattr(row, source_config["code_column"]),
                    "name_en": getattr(row, source_config.get("name_en_column", "name_en"), None) if source_config.get("name_en_column") else None,
                    "name_zh": getattr(row, source_config.get("name_zh_column", "name_zh"), None) if source_config.get("name_zh_column") else None,
                    "metadata_json": row[-1] if metadata_cols else None
                }
                entities.append(entity)

            return entities
        finally:
            session.close()

    def get_existing_entity_codes(self, entity_type: str) -> set:
        """
        Get set of existing entity codes from entities table.

        Args:
            entity_type: Type of entity

        Returns:
            Set of entity codes
        """
        session = self.Session()
        try:
            results = session.execute(text("""
                SELECT code FROM entities WHERE entity_type = :entity_type
            """), {"entity_type": entity_type}).fetchall()

            return {row.code for row in results}
        finally:
            session.close()

    def batch_insert_entities(
        self,
        entity_type: str,
        entities: List[Dict[str, Any]],
        batch_size: Optional[int] = None
    ) -> int:
        """
        Task 2.1.5: Batch INSERT new entities into entities table.

        Args:
            entity_type: Type of entity
            entities: List of entity dicts to insert
            batch_size: Number of rows per batch (default: adapter.batch_size)

        Returns:
            Number of entities inserted
        """
        if not entities:
            return 0

        # Use adapter's batch size if not specified
        if batch_size is None:
            batch_size = self.adapter.batch_size

        session = self.Session()
        try:
            inserted_count = 0

            # Process in batches
            for i in range(0, len(entities), batch_size):
                batch = entities[i:i + batch_size]

                # Build INSERT statement
                values = []
                params = {}
                for idx, entity in enumerate(batch):
                    values.append(f"(:entity_type_{idx}, :code_{idx}, :name_en_{idx}, :name_zh_{idx}, :metadata_json_{idx})")
                    params[f"entity_type_{idx}"] = entity_type
                    params[f"code_{idx}"] = entity["code"]
                    params[f"name_en_{idx}"] = entity.get("name_en")
                    params[f"name_zh_{idx}"] = entity.get("name_zh")

                    # Use adapter to serialize JSON
                    metadata = entity.get("metadata_json")
                    metadata_serialized = self.adapter.serialize_json(metadata)
                    params[f"metadata_json_{idx}"] = metadata_serialized

                values_clause = ", ".join(values)

                # Use adapter-specific INSERT syntax
                if self.adapter.__class__.__name__ == "PostgreSQLAdapter":
                    query = f"""
                        INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json)
                        VALUES {values_clause}
                        ON CONFLICT (entity_type, code) DO NOTHING
                    """
                else:
                    # SQLite uses INSERT OR IGNORE
                    query = f"""
                        INSERT OR IGNORE INTO entities (entity_type, code, name_en, name_zh, metadata_json)
                        VALUES {values_clause}
                    """

                result = session.execute(text(query), params)
                inserted_count += result.rowcount

            session.commit()
            return inserted_count
        except Exception as e:
            session.rollback()
            logger.error(f"Batch insert failed: {e}")
            raise
        finally:
            session.close()

    def batch_update_entities(
        self,
        entity_type: str,
        entities: List[Dict[str, Any]],
        batch_size: Optional[int] = None
    ) -> int:
        """
        Task 2.1.6: Batch UPDATE modified entities in entities table.

        Args:
            entity_type: Type of entity
            entities: List of entity dicts to update
            batch_size: Number of rows per batch (default: adapter.batch_size)

        Returns:
            Number of entities updated
        """
        if not entities:
            return 0

        # Use adapter's batch size if not specified
        if batch_size is None:
            batch_size = self.adapter.batch_size

        session = self.Session()
        try:
            updated_count = 0

            # Process in batches
            for i in range(0, len(entities), batch_size):
                batch = entities[i:i + batch_size]

                for entity in batch:
                    # Use adapter to serialize JSON
                    metadata = entity.get("metadata_json")
                    metadata_serialized = self.adapter.serialize_json(metadata)

                    # Use adapter-specific UPDATE syntax
                    if self.adapter.__class__.__name__ == "PostgreSQLAdapter":
                        # PostgreSQL: use CAST AS jsonb
                        query = text("""
                            UPDATE entities
                            SET name_en = :name_en,
                                name_zh = :name_zh,
                                metadata_json = CAST(:metadata_json AS jsonb),
                                updated_at = NOW()
                            WHERE entity_type = :entity_type
                              AND code = :code
                              AND (
                                  name_en IS DISTINCT FROM :name_en
                                  OR name_zh IS DISTINCT FROM :name_zh
                                  OR CAST(metadata_json AS text) IS DISTINCT FROM :metadata_json_text
                              )
                        """)
                        params = {
                            "entity_type": entity_type,
                            "code": entity["code"],
                            "name_en": entity.get("name_en"),
                            "name_zh": entity.get("name_zh"),
                            "metadata_json": metadata_serialized,
                            "metadata_json_text": metadata_serialized
                        }
                    else:
                        # SQLite: store as TEXT, no CAST needed
                        query = text("""
                            UPDATE entities
                            SET name_en = :name_en,
                                name_zh = :name_zh,
                                metadata_json = :metadata_json,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE entity_type = :entity_type
                              AND code = :code
                              AND (
                                  name_en IS DISTINCT FROM :name_en
                                  OR name_zh IS DISTINCT FROM :name_zh
                                  OR metadata_json IS DISTINCT FROM :metadata_json_text
                              )
                        """)
                        params = {
                            "entity_type": entity_type,
                            "code": entity["code"],
                            "name_en": entity.get("name_en"),
                            "name_zh": entity.get("name_zh"),
                            "metadata_json": metadata_serialized,
                            "metadata_json_text": metadata_serialized
                        }

                    result = session.execute(query, params)
                    updated_count += result.rowcount

            session.commit()
            return updated_count
        except Exception as e:
            session.rollback()
            logger.error(f"Batch update failed: {e}")
            raise
        finally:
            session.close()

    def acquire_advisory_lock(self, entity_type: str, session: Session) -> bool:
        """
        Task 2.1.8: Acquire lock for entity_type using lock manager.

        Args:
            entity_type: Type of entity to lock
            session: SQLAlchemy session

        Returns:
            True if lock acquired, False otherwise
        """
        # Use lock manager to acquire lock (works for both PostgreSQL and SQLite)
        lock_name = f"sync:{entity_type}"
        return self.lock_manager.acquire(session, lock_name)

    def sync_entity_type(
        self,
        entity_type: str,
        force_full_sync: bool = False
    ) -> Dict[str, Any]:
        """
        Main sync method: synchronize entities for a given type.

        Args:
            entity_type: Type of entity to sync
            force_full_sync: If True, ignore last_sync_at and do full sync

        Returns:
            Dict with sync results (inserted_count, updated_count, etc.)
        """
        start_time = time.time()
        started_at = datetime.now(timezone.utc)

        # Get source configuration
        source_config = self.get_source_config(entity_type)
        if not source_config:
            raise ValueError(f"No source configuration found for entity_type: {entity_type}")

        if not source_config["enabled"]:
            logger.info(f"Sync disabled for entity_type: {entity_type}")
            return {
                "status": "disabled",
                "inserted_count": 0,
                "updated_count": 0,
                "deleted_count": 0,
                "error_count": 0
            }

        session = self.Session()
        try:
            # Acquire advisory lock
            if not self.acquire_advisory_lock(entity_type, session):
                logger.warning(f"Could not acquire lock for entity_type: {entity_type}")
                return {
                    "status": "locked",
                    "inserted_count": 0,
                    "updated_count": 0,
                    "deleted_count": 0,
                    "error_count": 0
                }

            # Determine sync strategy
            last_sync_at = None if force_full_sync else source_config.get("last_sync_at")
            has_updated_at = self.check_source_has_updated_at(
                source_config["source_table"],
                source_config.get("source_schema", "public")
            )

            # Fetch entities from source
            if has_updated_at and last_sync_at and not force_full_sync:
                # Task 2.1.2: Timestamp-based incremental sync
                logger.info(f"Using timestamp-based incremental sync for {entity_type}")
                source_entities = self.fetch_entities_incremental(source_config, last_sync_at)
            else:
                # Task 2.1.3: ID-list fallback
                logger.info(f"Using ID-list fallback sync for {entity_type}")
                source_entities = self.fetch_entities_by_id_list(source_config)

            # Get existing entity codes
            existing_codes = self.get_existing_entity_codes(entity_type)

            # Separate new and existing entities
            new_entities = []
            existing_entities = []

            for entity in source_entities:
                if entity["code"] in existing_codes:
                    existing_entities.append(entity)
                else:
                    new_entities.append(entity)

            # Task 2.1.5: Batch INSERT new entities
            inserted_count = self.batch_insert_entities(entity_type, new_entities)

            # Task 2.1.6: Batch UPDATE modified entities
            updated_count = self.batch_update_entities(entity_type, existing_entities)

            # Update last_sync_at
            session.execute(text("""
                UPDATE entity_sources
                SET last_sync_at = :now, updated_at = :now
                WHERE entity_type = :entity_type
            """), {"now": datetime.now(timezone.utc), "entity_type": entity_type})

            session.commit()

            duration_seconds = int(time.time() - start_time)

            # Log sync result
            self.log_sync_result(
                entity_type=entity_type,
                started_at=started_at,
                inserted_count=inserted_count,
                updated_count=updated_count,
                deleted_count=0,
                error_count=0,
                status="success",
                duration_seconds=duration_seconds
            )

            return {
                "status": "success",
                "inserted_count": inserted_count,
                "updated_count": updated_count,
                "deleted_count": 0,
                "error_count": 0,
                "duration_seconds": duration_seconds
            }

        except Exception as e:
            session.rollback()
            duration_seconds = int(time.time() - start_time)

            # Log error
            self.log_sync_result(
                entity_type=entity_type,
                started_at=started_at,
                inserted_count=0,
                updated_count=0,
                deleted_count=0,
                error_count=1,
                status="failed",
                error_message=str(e),
                duration_seconds=duration_seconds
            )

            logger.error(f"Sync failed for {entity_type}: {e}")
            raise
        finally:
            # Release lock (important for file-based locks in SQLite)
            lock_name = f"sync:{entity_type}"
            self.lock_manager.release(session, lock_name)
            session.close()

    def log_sync_result(
        self,
        entity_type: str,
        started_at: datetime,
        inserted_count: int,
        updated_count: int,
        deleted_count: int,
        error_count: int,
        status: str,
        error_message: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        scheduler_id: Optional[int] = None
    ):
        """
        Task 2.2.2: Log sync result to entity_sync_logs table.

        Args:
            entity_type: Type of entity
            started_at: Sync start timestamp
            inserted_count: Number of entities inserted
            updated_count: Number of entities updated
            deleted_count: Number of entities deleted
            error_count: Number of errors encountered
            status: Sync status ('success', 'partial', 'failed')
            error_message: Error message if status is 'failed'
            duration_seconds: Sync duration in seconds
            scheduler_id: Scheduler ID if triggered by scheduler
        """
        session = self.Session()
        try:
            session.execute(text("""
                INSERT INTO entity_sync_logs (
                    entity_type, started_at, finished_at,
                    inserted_count, updated_count, deleted_count,
                    error_count, status, error_message,
                    duration_seconds, scheduler_id
                ) VALUES (
                    :entity_type, :started_at, :finished_at,
                    :inserted_count, :updated_count, :deleted_count,
                    :error_count, :status, :error_message,
                    :duration_seconds, :scheduler_id
                )
            """), {
                "entity_type": entity_type,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc),
                "inserted_count": inserted_count,
                "updated_count": updated_count,
                "deleted_count": deleted_count,
                "error_count": error_count,
                "status": status,
                "error_message": error_message,
                "duration_seconds": duration_seconds,
                "scheduler_id": scheduler_id
            })
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to log sync result: {e}")
        finally:
            session.close()

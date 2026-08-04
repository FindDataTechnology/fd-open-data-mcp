"""
Scheduler for entity sync operations.
Supports both cron-based and interval-based scheduling.
"""
import os
import time
import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

try:
    from croniter import croniter
except ImportError:
    croniter = None
    logging.warning("croniter not installed. Cron scheduling will not work.")

logger = logging.getLogger(__name__)


class EntitySyncScheduler:
    """
    Scheduler for entity sync operations.
    Runs as a daemon process, checking for scheduled syncs and executing them.
    """

    def __init__(self, database_url: str, check_interval_seconds: int = 60):
        """
        Initialize the scheduler.

        Args:
            database_url: PostgreSQL connection string
            check_interval_seconds: How often to check for scheduled syncs (default: 60s)
        """
        self.database_url = database_url
        self.check_interval_seconds = check_interval_seconds
        self.engine = create_engine(database_url)
        self.Session = sessionmaker(bind=self.engine)

    def calculate_next_run(
        self,
        schedule_type: str,
        cron_expr: Optional[str] = None,
        interval_minutes: Optional[int] = None,
        from_time: Optional[datetime] = None
    ) -> datetime:
        """
        Calculate the next run time based on schedule type.

        Args:
            schedule_type: 'cron' or 'interval'
            cron_expr: Cron expression (e.g., '0 2 * * *' for daily at 2 AM)
            interval_minutes: Interval in minutes
            from_time: Base time for calculation (default: now)

        Returns:
            Next run datetime
        """
        if from_time is None:
            from_time = datetime.utcnow()

        if schedule_type == 'cron':
            if croniter is None:
                raise ValueError("croniter not installed. Cannot use cron scheduling.")
            if not cron_expr:
                raise ValueError("cron_expr required for cron schedule type")

            cron = croniter(cron_expr, from_time)
            return cron.get_next(datetime)

        elif schedule_type == 'interval':
            if not interval_minutes:
                raise ValueError("interval_minutes required for interval schedule type")
            return from_time + timedelta(minutes=interval_minutes)

        else:
            raise ValueError(f"Unknown schedule_type: {schedule_type}")

    def get_due_schedules(self) -> list[dict]:
        """
        Get all schedules that are due to run.

        Returns:
            List of schedule dicts with entity_type and schedule info
        """
        session = self.Session()
        try:
            result = session.execute(text("""
                SELECT
                    entity_type,
                    schedule_type,
                    cron_expr,
                    interval_minutes,
                    next_run_at,
                    last_run_at
                FROM entity_sync_schedules
                WHERE enabled = TRUE
                  AND next_run_at <= NOW()
                ORDER BY next_run_at ASC
            """))

            schedules = []
            for row in result:
                schedules.append({
                    "entity_type": row.entity_type,
                    "schedule_type": row.schedule_type,
                    "cron_expr": row.cron_expr,
                    "interval_minutes": row.interval_minutes,
                    "next_run_at": row.next_run_at,
                    "last_run_at": row.last_run_at
                })

            return schedules

        finally:
            session.close()

    def update_schedule_after_sync(self, entity_type: str, schedule: dict):
        """
        Update schedule after sync completes.

        Args:
            entity_type: Entity type that was synced
            schedule: Schedule dict with schedule_type, cron_expr, interval_minutes
        """
        session = self.Session()
        try:
            now = datetime.utcnow()
            next_run = self.calculate_next_run(
                schedule_type=schedule['schedule_type'],
                cron_expr=schedule.get('cron_expr'),
                interval_minutes=schedule.get('interval_minutes'),
                from_time=now
            )

            session.execute(text("""
                UPDATE entity_sync_schedules
                SET last_run_at = :last_run_at,
                    next_run_at = :next_run_at
                WHERE entity_type = :entity_type
            """), {
                "entity_type": entity_type,
                "last_run_at": now,
                "next_run_at": next_run
            })

            session.commit()
            logger.info(f"Updated schedule for {entity_type}: next_run_at={next_run}")

        except Exception as e:
            session.rollback()
            logger.error(f"Failed to update schedule for {entity_type}: {e}")
            raise
        finally:
            session.close()

    def run_sync_for_schedule(self, entity_type: str):
        """
        Run sync for a specific entity type.

        Args:
            entity_type: Entity type to sync
        """
        from fd_open_data_mcp.sync.engine import EntitySyncEngine

        logger.info(f"Starting sync for {entity_type}")

        try:
            engine = EntitySyncEngine(database_url=self.database_url)
            result = engine.sync_entity_type(entity_type)

            logger.info(
                f"Sync completed for {entity_type}: "
                f"status={result['status']}, "
                f"inserted={result['inserted_count']}, "
                f"updated={result['updated_count']}, "
                f"errors={result['error_count']}"
            )

            return result

        except Exception as e:
            logger.error(f"Sync failed for {entity_type}: {e}")
            raise

    def run_once(self):
        """
        Run one iteration of the scheduler.
        Check for due schedules and execute them.
        """
        logger.info("Scheduler checking for due schedules...")

        try:
            due_schedules = self.get_due_schedules()

            if not due_schedules:
                logger.info("No schedules due to run")
                return

            logger.info(f"Found {len(due_schedules)} schedules due to run")

            for schedule in due_schedules:
                entity_type = schedule['entity_type']

                try:
                    # Run sync
                    self.run_sync_for_schedule(entity_type)

                    # Update schedule
                    self.update_schedule_after_sync(entity_type, schedule)

                except Exception as e:
                    logger.error(f"Failed to process schedule for {entity_type}: {e}")
                    # Continue with other schedules
                    continue

        except Exception as e:
            logger.error(f"Scheduler iteration failed: {e}")
            raise

    def run_daemon(self):
        """
        Run the scheduler as a daemon process.
        Continuously checks for due schedules and executes them.
        """
        logger.info(f"Starting scheduler daemon (check_interval={self.check_interval_seconds}s)")

        try:
            while True:
                self.run_once()
                time.sleep(self.check_interval_seconds)

        except KeyboardInterrupt:
            logger.info("Scheduler daemon stopped by user")
        except Exception as e:
            logger.error(f"Scheduler daemon crashed: {e}")
            raise


def initialize_schedules(database_url: str):
    """
    Initialize default schedules for all entity types.

    Args:
        database_url: PostgreSQL connection string
    """
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Get all entity types
        result = session.execute(text("""
            SELECT entity_type FROM entity_sources WHERE enabled = TRUE
        """))

        entity_types = [row.entity_type for row in result]

        logger.info(f"Initializing schedules for {len(entity_types)} entity types")

        # Define default schedules
        # Critical types: hourly
        critical_types = ['stock', 'company']
        # Other types: daily
        other_types = [t for t in entity_types if t not in critical_types]

        now = datetime.utcnow()

        for entity_type in entity_types:
            if entity_type in critical_types:
                schedule_type = 'interval'
                interval_minutes = 60
                cron_expr = None
                next_run = now + timedelta(minutes=60)
            else:
                schedule_type = 'cron'
                cron_expr = '0 2 * * *'  # Daily at 2 AM
                interval_minutes = None
                if croniter:
                    cron = croniter(cron_expr, now)
                    next_run = cron.get_next(datetime)
                else:
                    # Fallback to daily interval if croniter not available
                    schedule_type = 'interval'
                    interval_minutes = 1440  # 24 hours
                    next_run = now + timedelta(minutes=1440)

            session.execute(text("""
                INSERT INTO entity_sync_schedules (
                    entity_type, schedule_type, cron_expr, interval_minutes,
                    next_run_at, enabled
                ) VALUES (
                    :entity_type, :schedule_type, :cron_expr, :interval_minutes,
                    :next_run_at, TRUE
                )
                ON CONFLICT (entity_type) DO NOTHING
            """), {
                "entity_type": entity_type,
                "schedule_type": schedule_type,
                "cron_expr": cron_expr,
                "interval_minutes": interval_minutes,
                "next_run_at": next_run
            })

        session.commit()
        logger.info("Schedules initialized successfully")

    except Exception as e:
        session.rollback()
        logger.error(f"Failed to initialize schedules: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    # Initialize schedules
    database_url = os.environ.get(
        "FD_OPEN_DATA_MCP_DATABASE_URL",
        "postgresql://admin:admin123@192.168.1.4:5433/postgres"
    )

    initialize_schedules(database_url)

    # Run scheduler daemon
    scheduler = EntitySyncScheduler(database_url=database_url, check_interval_seconds=60)
    scheduler.run_daemon()

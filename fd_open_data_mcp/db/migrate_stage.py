"""Deploy-time schema migration stage.

Runs ``alembic upgrade head`` to completion before the application container
starts; intended to be the Deployment's initContainer::

    python -m fd_open_data_mcp.db.migrate_stage

Behaviour contract (k8s/zihan/20-fd-open-data-mcp.yaml):

* The database is taken from ``FD_OPEN_DATA_MCP_DATABASE_URL`` — the same
  source the server container reads. NOTE: importing this module pulls in
  ``fd_open_data_mcp.db``, whose dotenv loading can override the variable
  with a repo-local ``.env.local`` when run from the project root (same
  precedence the application engine itself uses); deploy images carry no
  .env files, so the initContainer sees only its envFrom Secret.
* Before touching the schema the stage takes a session-level Postgres
  advisory lock ``pg_advisory_lock(hashtext('fd_mcp_schema_migrate'))`` and
  holds it for the whole run, so two migrators starting concurrently (rollout
  surge, scale-up) serialize instead of racing. The key is deliberately
  distinct from ``fd_mcp_uq_sem_obs_swap`` (fd_open_data_mcp/migrate.py):
  each mechanism locks independently.
* Any failure (unreachable database, failed DDL) exits non-zero: the
  initContainer fails, the pod never starts, and the rollout stops while the
  previous ReplicaSet keeps serving.
* Idempotent: when the database is already at head, alembic applies nothing
  and the stage exits 0.

PostgreSQL only: the baseline revision executes a pg_dump snapshot, and the
advisory lock is Postgres-specific. SQLite consumers build their schema with
create_all(), not with this stage.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

LOGGER = logging.getLogger("fd_open_data_mcp.db.migrate_stage")

DATABASE_URL_ENV = "FD_OPEN_DATA_MCP_DATABASE_URL"
ALEMBIC_DIR_ENV = "FD_OPEN_DATA_MCP_ALEMBIC_DIR"

# Session-level advisory lock serializing concurrent migration stages. Must
# stay distinct from every other advisory-lock key in the workspace.
ADVISORY_LOCK_KEY = "fd_mcp_schema_migrate"


def _resolve_alembic_dir() -> Path:
    """Locate the alembic script directory.

    Resolution order: ``FD_OPEN_DATA_MCP_ALEMBIC_DIR`` -> ``<cwd>/alembic``
    (the image sets WORKDIR /app and COPYs the chain to /app/alembic) ->
    ``<repo root>/alembic`` relative to this file (source checkout).
    """
    candidates = []
    env_dir = os.environ.get(ALEMBIC_DIR_ENV)
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path.cwd() / "alembic")
    candidates.append(Path(__file__).resolve().parents[2] / "alembic")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    tried = ", ".join(str(c) for c in candidates)
    raise RuntimeError(
        f"alembic script directory not found (tried: {tried}); "
        f"set {ALEMBIC_DIR_ENV} or run with cwd at the project root"
    )


def _masked_url(database_url: str) -> str:
    """URL for logs with the password hidden — initContainer logs must not
    carry credentials."""
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - masking must never break the run
        return "<unparseable database url>"


def _lock_engine(database_url: str) -> Engine:
    """Dedicated connection that holds the advisory lock while alembic runs
    on its own connections. AUTOCOMMIT so the lock session never sits idle
    in an open transaction; NullPool so it is exactly one connection."""
    return create_engine(
        database_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args={"connect_timeout": 10, "application_name": "fd-mcp-migrate-stage"},
    )


def run() -> None:
    """Acquire the advisory lock, run ``alembic upgrade head``, release."""
    from alembic import command
    from alembic.config import Config

    database_url = os.environ.get(DATABASE_URL_ENV)
    if not database_url:
        raise RuntimeError(f"{DATABASE_URL_ENV} is not set; cannot migrate")
    if database_url.startswith("sqlite"):
        raise RuntimeError(
            "the migration stage is PostgreSQL-only "
            "(baseline revision is a pg_dump snapshot; advisory locks are "
            "Postgres-specific); SQLite consumers use create_all()"
        )

    alembic_dir = _resolve_alembic_dir()
    engine = _lock_engine(database_url)
    try:
        with engine.connect() as lock_conn:
            LOGGER.info(
                "acquiring advisory lock %r on %s",
                ADVISORY_LOCK_KEY,
                _masked_url(database_url),
            )
            lock_conn.exec_driver_sql(
                f"SELECT pg_advisory_lock(hashtext('{ADVISORY_LOCK_KEY}'))"
            )
            try:
                LOGGER.info("running alembic upgrade head (script: %s)", alembic_dir)
                cfg = Config()  # no .ini: programmatic config only
                cfg.set_main_option("script_location", str(alembic_dir))
                # Escape '%' for configparser interpolation (URL passwords).
                cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
                command.upgrade(cfg, "head")
            finally:
                # Session-level lock: closing the connection would also
                # release it, but unlock explicitly so the release is visible
                # in the log trail rather than implied by teardown.
                try:
                    lock_conn.exec_driver_sql(
                        f"SELECT pg_advisory_unlock(hashtext('{ADVISORY_LOCK_KEY}'))"
                    )
                    LOGGER.info("advisory lock %r released", ADVISORY_LOCK_KEY)
                except Exception:  # noqa: BLE001 - never mask the real error
                    LOGGER.warning("advisory lock unlock failed; connection teardown releases it", exc_info=True)
    finally:
        engine.dispose()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-5.5s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("alembic.runtime.migration").setLevel(logging.INFO)
    try:
        run()
    except Exception:
        LOGGER.exception("schema migration stage FAILED")
        return 1
    LOGGER.info("schema migration stage complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

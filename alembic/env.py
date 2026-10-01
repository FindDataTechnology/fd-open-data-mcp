"""Alembic environment for fd-open-data-mcp migrations."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import models to get metadata
from fd_open_data_mcp.models import Base  # noqa

# This is the Alembic Config object
config = context.config

# Set database URL from env var or default
database_url = os.environ.get(
    "FD_OPEN_DATA_MCP_DATABASE_URL",
    "postgresql://fd:FD_PG_PASSWORD@guangzhou-xinru:30432/fd_open_data"
)
config.set_main_option("sqlalchemy.url", database_url)

# Interpret config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Tables with NO model in this repository. They are created by the chain
# itself (registry_entries since 0003's guarded section, per the
# schema-drift-closure adoption; registry_indicator_embeddings by 0003's DDL
# contract) but read/written through raw SQL, so without this exemption
# autogenerate/`alembic check` would propose DROP TABLE for them. This list
# no longer masks model drift: every model-declared table was adopted into
# the chain by 0006_control_plane_adoption and is fully compared.
_EXTERNALLY_OWNED_NO_MODEL = frozenset(
    {
        "registry_entries",
        "registry_indicator_embeddings",
    }
)


def _include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in _EXTERNALLY_OWNED_NO_MODEL:
        return False
    # 0003's expand-contract vector columns: DDL-contract only, deliberately
    # not declared on the models (pgvector types are read via raw SQL in
    # vector_backend); their HNSW indexes ride along.
    if type_ == "column" and (obj.table.name, name) in _CONTRACT_COLUMNS:
        return False
    if type_ == "index" and name in _CONTRACT_INDEXES:
        return False
    return True


_CONTRACT_COLUMNS = frozenset(
    {
        ("concept_embeddings", "embedding_vec"),
        ("entity_embeddings", "embedding_vec"),
    }
)
_CONTRACT_INDEXES = frozenset(
    {
        "hnsw_concept_embeddings_vec",
        "hnsw_entity_embeddings_vec",
    }
)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

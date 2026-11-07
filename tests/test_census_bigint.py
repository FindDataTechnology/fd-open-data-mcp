"""widen-census-bigint: the census size counters must survive a >2GB store.

Found live 2026-11-08: every reconciler tick logged

    WARNING census refresh skipped: (psycopg2.errors.NumericValueOutOfRange)
    integer out of range
    UPDATE data_census SET approx_rows=... total_size_bytes=...

with ``total_size_bytes = 2,873,810,944`` (int4 max is 2,147,483,647), so
``data_census.sampled_at`` had been frozen at 2026-11-03 18:07 for five days.
"""
from __future__ import annotations

import sqlalchemy as sa

from fd_open_data_mcp.models import Base, DataCensus


def test_census_counters_are_64_bit_columns():
    cols = DataCensus.__table__.c
    assert isinstance(cols.approx_rows.type, sa.BigInteger), cols.approx_rows.type
    assert isinstance(cols.total_size_bytes.type, sa.BigInteger), cols.total_size_bytes.type


def test_census_row_round_trips_above_int4_range(session):
    """The exact value that broke production must persist and read back."""
    huge_bytes = 2_873_810_944          # the live value that raised the error
    huge_rows = 3_000_000_000           # above 2**31, as a shard can report
    row = DataCensus(store="probe-local", kind="local", approx_rows=huge_rows,
                     exact=False, total_size_bytes=huge_bytes)
    session.add(row)
    session.commit()
    session.expire_all()
    got = session.query(DataCensus).filter_by(store="probe-local").one()
    assert got.total_size_bytes == huge_bytes
    assert got.approx_rows == huge_rows


def test_migration_revision_chains_from_head():
    """0017 must hang off the current head, or `alembic upgrade head` skips it."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic/versions/0017_census_bigint.py"
    spec = importlib.util.spec_from_file_location("_mig0017", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0017_census_bigint"
    assert mod.down_revision == "0016_policy_launcher"

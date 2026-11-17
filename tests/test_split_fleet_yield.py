"""split-fleet-yield-by-executor 1.x/2.x — the total stays, the split is honest.

1.1/1.2 pin the read-time LEFT JOIN: a scrapy run and a direct run land in
their own buckets, a run whose policy row is gone lands in `unattributed`
instead of vanishing, and `sum(buckets) + unattributed == rows_new` always.
2.1/2.2 pin the rendered digest line: it names the quantity "rows landed",
prints both buckets, and a dead day still reads 0 everywhere.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.visibility import digest, snapshot


def _mk_policy(session, name, executor="scrapy"):
    p = CrawlPolicy(
        name=name, enabled=True, concept_ids=[1], entity_type="fund",
        cron_expr="0 6 * * *", timezone="UTC",
        date_policy={"mode": "since_last"}, frequency="daily", mode="per_date",
        executor=executor,
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _mk_run(session, policy, *, attempted, new):
    r = PolicyRun(
        policy_id=policy.id if policy else None, origin="policy" if policy else "adhoc",
        status="success",
        started_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
        rows_attempted=attempted, rows_new=new,
    )
    session.add(r)
    session.commit()
    return r


def _insert_orphan_runs(session, count, *, new):
    """Runs whose policy_id points to a row that no longer exists.

    The test fixture turns PRAGMA foreign_keys=ON, so a dangling policy_id
    cannot be inserted through the ORM. Insert through a separate sqlite3
    connection (FK off by default) against the same file — the LEFT JOIN must
    see them exactly as if the policy row had been deleted.
    """
    db_path = session.get_bind().url.database
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat()
    con = sqlite3.connect(db_path)
    try:
        for _ in range(count):
            con.execute(
                "INSERT INTO policy_runs "
                "(policy_id, origin, status, started_at, rows_attempted, rows_new) "
                "VALUES (?, 'policy', 'success', ?, ?, ?)",
                (999999, now, new, new),
            )
        con.commit()
    finally:
        con.close()


def test_1_1_split_by_executor_keeps_total_and_invariant(session):
    scrapy = _mk_policy(session, "sc", executor="scrapy")
    direct = _mk_policy(session, "dr", executor="direct")
    _mk_run(session, scrapy, attempted=5, new=5)
    _mk_run(session, direct, attempted=100, new=100)

    fy = snapshot.fleet_yield(session, hours=24)
    assert fy["rows_new"] == 105
    assert fy["by_executor"] == {"scrapy": 5, "direct": 100}
    assert fy["rows_new_unattributed"] == 0
    assert sum(fy["by_executor"].values()) + fy["rows_new_unattributed"] == fy["rows_new"]


def test_1_2_orphan_policy_rows_are_unattributed_not_dropped(session):
    scrapy = _mk_policy(session, "sc", executor="scrapy")
    _mk_run(session, scrapy, attempted=5, new=5)
    _insert_orphan_runs(session, 7, new=1)

    fy = snapshot.fleet_yield(session, hours=24)
    assert fy["rows_new_unattributed"] == 7, "7 orphan runs must not vanish"
    assert fy["rows_attempted_unattributed"] == 7
    assert fy["rows_new"] == 12, "total still sums every run"
    assert sum(fy["by_executor"].values()) + fy["rows_new_unattributed"] == fy["rows_new"]

    # Negative proof: an INNER JOIN would silently drop the orphan runs.
    from sqlalchemy import func
    inner = (
        session.query(func.coalesce(func.sum(PolicyRun.rows_new), 0))
        .join(CrawlPolicy, PolicyRun.policy_id == CrawlPolicy.id)
        .filter(PolicyRun.status != "running")
        .scalar()
    )
    assert inner == 5, "INNER JOIN is exactly the failure mode this change avoids"


def test_1_2_zero_window_has_empty_buckets(session):
    fy = snapshot.fleet_yield(session, hours=24)
    assert fy["rows_new"] == 0
    assert fy["by_executor"] == {"scrapy": 0, "direct": 0}
    assert fy["rows_new_unattributed"] == 0


def _render(fy):
    snap = {"fleet_yield": fy, "recent_runs": [], "summary": {},
            "per_source_outcome": [], "circuit_state": []}
    return digest.format_digest(snap, ZoneInfo("UTC"))[1]


def test_2_1_digest_names_rows_landed_with_both_buckets(session):
    scrapy = _mk_policy(session, "sc", executor="scrapy")
    direct = _mk_policy(session, "dr", executor="direct")
    _mk_run(session, scrapy, attempted=5, new=5)
    _mk_run(session, direct, attempted=100, new=100)

    line = _render(snapshot.fleet_yield(session, hours=24))
    assert "ACQUIRED 105 rows landed" in line
    assert "scrapy crawls 5" in line
    assert "direct scripts 100" in line
    assert "new rows" not in line, "the overstating label must be gone"
    assert "unattributed" not in line, "zero residue must not teach a phantom bucket"


def test_2_1_digest_shows_unattributed_only_when_nonzero(session):
    scrapy = _mk_policy(session, "sc", executor="scrapy")
    _mk_run(session, scrapy, attempted=5, new=5)
    _insert_orphan_runs(session, 7, new=1)

    line = _render(snapshot.fleet_yield(session, hours=24))
    assert "unattributed 7" in line
    assert "new rows" not in line


def test_2_2_zero_day_reads_zero_in_every_bucket(session):
    line = _render(snapshot.fleet_yield(session, hours=24))
    assert "ACQUIRED 0 rows landed" in line
    assert "scrapy crawls 0" in line
    assert "direct scripts 0" in line

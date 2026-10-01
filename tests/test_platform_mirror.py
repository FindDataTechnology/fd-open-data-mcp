"""concept-platform-federation: crawl_runs mirroring + crawl_sources registration (rows_written: prod NOT NULL DEFAULT 0 — unknown records 0).

Spec: openspec/changes/concept-platform-integration (capability
concept-platform-federation). Covers: status mapping incl. zero-yield ->
success rows=0, single-source exact rows, multi-source split with NULL rows,
refusal rows never mirrored, mirror failure swallowed, registration
insert-only idempotency, and the two close-site wirings (probe + cancel).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from fd_open_data_mcp.models import (CrawlPolicy, CrawlRun, CrawlSource, CrawlSite,
                                     PolicyRun, Source)
from fd_open_data_mcp.refresh.platform_mirror import (mirror_run_close,
                                                      register_concept_sources,
                                                      run_sources)
from fd_open_data_mcp.refresh.reconciler import reconcile_once
from fd_open_data_mcp.refresh.runs import cancel_run

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
NAIVE_NOW = NOW.replace(tzinfo=None)


def _plan_json(sources=("akshare", "datacommons")):
    return {
        "wanted_concepts": [
            {"concept_id": 1, "code": "cpi.yoy", "entity_type": "country",
             "ranked_sources": [{"source": s, "score": 1.0} for s in sources]},
            {"concept_id": 2, "code": "gdp", "entity_type": "country",
             "ranked_sources": [{"source": sources[0], "score": 0.9}]},
        ],
        "entity_scope": {"entity_type": "country", "entity_ids": None},
        "date_range": {"start": "2026-09-01", "end": "2026-09-10"},
    }


def _policy(session, source_filter=None):
    p = CrawlPolicy(name="p-mirror", concept_ids=[1, 2], entity_type="country",
                    date_policy={"mode": "trailing", "days": 7},
                    source_filter=source_filter, cron_expr="0 0 1 1 *")
    session.add(p)
    session.commit()
    return p


def _run(session, policy=None, status="success", plan_json=None, job_ref="j-1",
         rows_new=None, rows_attempted=None, detail=None, origin="policy"):
    run = PolicyRun(policy_id=policy.id if policy else None, origin=origin,
                    status=status, plan_json=plan_json, job_ref=job_ref,
                    started_at=NAIVE_NOW - timedelta(hours=1),
                    finished_at=NAIVE_NOW, rows_new=rows_new,
                    rows_attempted=rows_attempted, detail=detail)
    session.add(run)
    session.commit()
    return run


# ── run_sources (task 1.3) ───────────────────────────────────────────────────
def test_run_sources_single_source_filter_wins(session):
    p = _policy(session, source_filter=["wbgapi"])
    run = _run(session, p, plan_json=_plan_json(("akshare", "edgar")))
    assert run_sources(run) == ["wbgapi"]


def test_run_sources_plan_json_dedup_stable(session):
    run = _run(session, plan_json=_plan_json(("akshare", "datacommons")))
    assert run_sources(run) == ["akshare", "datacommons"]


def test_run_sources_adhoc_no_policy_no_plan(session):
    run = _run(session, origin="adhoc", plan_json=None)
    assert run_sources(run) == []


# ── mirror_run_close (task 1.1) ──────────────────────────────────────────────
def test_mirror_single_source_reports_exact_rows(session):
    p = _policy(session, source_filter=["akshare"])
    run = _run(session, p, status="success", plan_json=_plan_json(("akshare",)), rows_new=37)
    assert mirror_run_close(session, run) == 1
    row = session.query(CrawlRun).one()
    assert (row.source, row.kind, row.status) == ("akshare", "concept", "success")
    assert row.rows_written == 37
    assert row.error_head is None


def test_mirror_multi_source_splits_rows_zero(session):
    # prod crawl_runs.rows_written is NOT NULL DEFAULT 0; unknown multi-source
    # attribution records 0 (platform convention), never NULL
    run = _run(session, plan_json=_plan_json(("akshare", "datacommons")),
               status="success", rows_new=500)
    assert mirror_run_close(session, run) == 2
    rows = session.query(CrawlRun).order_by(CrawlRun.source).all()
    assert [r.source for r in rows] == ["akshare", "datacommons"]
    assert all(r.status == "success" and r.rows_written == 0 for r in rows)


def test_mirror_single_source_unreported_rows_zero(session):
    # single source but the pod never reported counters -> unknown -> 0
    p = _policy(session, source_filter=["akshare"])
    run = _run(session, p, status="zero_yield", plan_json=_plan_json(("akshare",)),
               rows_new=None)
    assert mirror_run_close(session, run) == 1
    assert session.query(CrawlRun).one().rows_written == 0


def test_mirror_zero_yield_success_with_zero_rows(session):
    run = _run(session, plan_json=_plan_json(("akshare", "datacommons")),
               status="zero_yield", rows_new=0, rows_attempted=0)
    assert mirror_run_close(session, run) == 2
    rows = session.query(CrawlRun).all()
    assert all(r.status == "success" and r.rows_written == 0 for r in rows)


def test_mirror_failed_keeps_error_head(session):
    p = _policy(session, source_filter=["edgar"])
    run = _run(session, p, status="failed", plan_json=_plan_json(("edgar",)),
               detail="boom: upstream 500")
    assert mirror_run_close(session, run) == 1
    row = session.query(CrawlRun).one()
    assert row.status == "failed"
    assert row.error_head == "boom: upstream 500"


def test_mirror_never_executed_rows_are_not_reported(session):
    # refusal / launch-failure / frozen rows are born closed without job_ref
    p = _policy(session)
    refused = _run(session, p, status="failed", plan_json=_plan_json(), job_ref=None,
                   detail="refused: guardrail")
    assert mirror_run_close(session, refused) == 0
    open_run = _run(session, p, status="running", job_ref="j-open")
    assert mirror_run_close(session, open_run) == 0
    assert session.query(CrawlRun).count() == 0


def test_mirror_unattributable_run_left_blank(session):
    # direct-script run: no plan_json, no single-source filter
    p = _policy(session)
    run = _run(session, p, status="success", plan_json=None, rows_new=10)
    assert mirror_run_close(session, run) == 0
    assert session.query(CrawlRun).count() == 0


def test_mirror_failure_is_swallowed(session, monkeypatch):
    run = _run(session, plan_json=_plan_json(), rows_new=1)

    def boom(*a, **k):
        raise RuntimeError("mirror channel down")

    monkeypatch.setattr("fd_open_data_mcp.refresh.platform_mirror.run_sources", boom)
    assert mirror_run_close(session, run) == 0  # no raise


def test_mirror_failure_never_poisons_caller_session(session, monkeypatch):
    """2026-09-27 incident regression: a rejected crawl_runs insert must not
    roll back the caller's session. The mirror writes in its own transaction;
    the caller must still be able to commit its own work afterwards."""
    p = _policy(session, source_filter=["akshare"])
    run = _run(session, p, status="success", plan_json=_plan_json(("akshare",)),
               rows_new=9)

    class _BoomSession:
        def add(self, *a, **k):
            raise RuntimeError("insert rejected (simulated NotNullViolation)")

        def commit(self):
            raise RuntimeError("session is poisoned")

        def close(self):
            pass

    monkeypatch.setattr("fd_open_data_mcp.refresh.platform_mirror.Session",
                        lambda bind=None: _BoomSession())
    assert mirror_run_close(session, run) == 0
    # caller session still usable: the probe close it holds commits fine
    run.status = "failed"
    session.commit()
    assert session.get(PolicyRun, run.id).status == "failed"


def test_crawl_run_rows_written_matches_prod_schema(session):
    """Model must match prod's NOT NULL DEFAULT 0 — the schema drift that was
    missing on 2026-09-27 let sqlite fixtures accept NULLs prod rejects."""
    from sqlalchemy import inspect

    col = {c["name"]: c for c in inspect(session.get_bind()).get_columns("crawl_runs")}["rows_written"]
    assert col["nullable"] is False
    assert col["default"] is not None  # DEFAULT 0 present in the DDL
    # writers that never set the column land as 0, never NULL (started_at is
    # NOT NULL with no default, same as prod — the row must state it)
    session.add(CrawlRun(source="akshare", kind="concept", status="success",
                         started_at=NAIVE_NOW))
    session.commit()
    assert session.query(CrawlRun).one().rows_written == 0
    # an explicit None also lands as the default, never NULL
    session.add(CrawlRun(source="akshare", kind="concept", status="success",
                         started_at=NAIVE_NOW, rows_written=None))
    session.commit()
    assert [r.rows_written for r in session.query(CrawlRun).all()] == [0, 0]


# ── close-site wiring (task 1.2) ─────────────────────────────────────────────
class _FakeLauncher:
    def __init__(self, poll_state="unknown"):
        self.poll_state = poll_state

    def launch(self, plan, policy):
        return ("job-x", None)

    def poll(self, job_ref):
        return self.poll_state

    def delete(self, job_ref):
        return True


def test_probe_close_mirrors_into_crawl_runs(session):
    p = _policy(session)
    run = _run(session, p, status="running", plan_json=_plan_json(("akshare",)),
               rows_new=5, rows_attempted=5)
    run.finished_at = None
    session.commit()
    summary = reconcile_once(session, _FakeLauncher(poll_state="success"), now=NOW)
    assert summary["probed_closed"] == 1
    assert run.status == "success"
    row = session.query(CrawlRun).one()
    assert (row.source, row.kind, row.status, row.rows_written) == \
        ("akshare", "concept", "success", 5)


def test_cancel_run_mirrors_into_crawl_runs(session):
    p = _policy(session, source_filter=["wbgapi"])
    run = _run(session, p, status="running", plan_json=_plan_json(("wbgapi",)))
    out = cancel_run(session, run.id, actor="panel-test",
                     launcher=_FakeLauncher(), now=NOW)
    assert out["status"] == "cancelled"
    row = session.query(CrawlRun).one()
    assert (row.source, row.status) == ("wbgapi", "cancelled")


# ── registration (task 2.1) ──────────────────────────────────────────────────
def test_register_concept_sources_idempotent(session):
    session.add(CrawlSite(id="tencent"))  # FK precondition (prod ships it)
    session.add_all([Source(name="akshare", label="AKShare"),
                     Source(name="wbgapi", label="World Bank API")])
    # pre-existing platform row must not be touched (same-named content-repo source)
    session.add(CrawlSource(source="wbgapi", site="tencent", schedule="0 3 * * *",
                            enabled=True, last_commit="abc123"))
    session.commit()

    first = register_concept_sources(session)
    assert first == {"inserted": 1, "existing": 1, "site": "tencent"}
    row = session.query(CrawlSource).filter_by(source="akshare").one()
    assert (row.site, row.schedule, row.enabled) == ("tencent", None, True)
    kept = session.query(CrawlSource).filter_by(source="wbgapi").one()
    assert kept.schedule == "0 3 * * *" and kept.last_commit == "abc123"  # untouched

    again = register_concept_sources(session)
    assert again == {"inserted": 0, "existing": 2, "site": "tencent"}
    assert session.query(CrawlSource).count() == 2

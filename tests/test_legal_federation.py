r"""Legal-line federation tests (change legal-line-federation, tasks 1.1/1.3/1.4/4.1).

Four layers, pinned the same ways as the 0006 contract tests:

- Migration contract (0007_legal_federation): sqlite executes nothing (dialect
  guard); against a recording fake-PG bind the revision emits only guarded
  statements (ADD COLUMN IF NOT EXISTS / ON CONFLICT) and re-emits the exact
  same list on a second run — the idempotent-upsert seed by construction.
- Seed pinning: the 8 law-line members, their runner declarations read from
  helm-law-scraw values.yaml (all on the ccr wave image — flk diverges from
  the chart's Harbor ref on purpose since dispatcher Jobs carry no
  imagePullSecrets; rmfyalk takes the auth-broker image with
  --account-id/--auth-broker; wenshu frozen with no declaration), and the
  upsert never touches dispatcher-owned fields.
- PostgreSQL functional (local scratch server, skipped when unreachable):
  'alembic upgrade head' builds the chain including 0007; re-executing the
  revision's statements is a verified no-op that leaves the 8 rows, their
  values, and a dispatcher-owned field (schedule) untouched.
- Application layer on the sqlite fixture: the model columns round-trip, the
  shared trigger op releases declared federated members (queue site =
  registered site) while refusing frozen / declaration-less / unregistered /
  single-flighted sources each with its own reason, and the panel renders
  kind, the freeze, the runner summary and run metrics.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import socket
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text

from fd_open_data_mcp.panel.app import app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = PROJECT_ROOT / "alembic"
REVISION_PATH = ALEMBIC_DIR / "versions" / "0007_legal_federation.py"

SITE = "xinru-master"
FROZEN = "月度语料包路线（夜跑永久停）"


def _load_revision():
    spec = importlib.util.spec_from_file_location("mig_0007", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mig():
    return _load_revision()


# ---------------------------------------------------------------------------
# Chain wiring
# ---------------------------------------------------------------------------

def test_revision_wiring():
    script = ScriptDirectory(str(ALEMBIC_DIR))
    assert script.get_current_head() == "0007_legal_federation"
    assert script.get_revision("0007_legal_federation").down_revision == (
        "0006_control_plane_adoption")


# ---------------------------------------------------------------------------
# SQLite guard: bound to a real sqlite connection, executes nothing
# ---------------------------------------------------------------------------

def _sqlite_ops(monkeypatch, mig):
    engine = create_engine("sqlite://")
    conn = engine.connect()
    ctx = MigrationContext.configure(conn)
    monkeypatch.setattr(mig, "op", Operations(ctx))
    executed = []
    event.listen(conn, "before_cursor_execute", lambda c, *a, **k: executed.append(a[1]))
    return engine, conn, executed


def test_upgrade_and_downgrade_are_noop_on_sqlite(mig, monkeypatch):
    engine, conn, executed = _sqlite_ops(monkeypatch, mig)
    try:
        mig.upgrade()
        mig.downgrade()
        assert executed == [], "must not touch a non-postgresql database"
    finally:
        conn.close()
        engine.dispose()


def test_guard_returns_before_any_sql(mig):
    broken = types.SimpleNamespace(
        dialect=types.SimpleNamespace(name="mysql"),
        exec_driver_sql=lambda *_: pytest.fail("guard must return before any SQL"),
    )
    original = mig.op
    mig.op = types.SimpleNamespace(get_bind=lambda: broken)
    try:
        mig.upgrade()
        mig.downgrade()
    finally:
        mig.op = original


# ---------------------------------------------------------------------------
# Fake PG bind: the guarded statements the revision must emit
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.dialect = types.SimpleNamespace(name="postgresql")
        self.statements = []

    def exec_driver_sql(self, sql):
        self.statements.append(sql)


def _run_on_fake_pg(mig, fn="upgrade"):
    bind = _Recorder()
    original = mig.op
    mig.op = types.SimpleNamespace(get_bind=lambda: bind)
    try:
        getattr(mig, fn)()
    finally:
        mig.op = original
    return bind


def test_upgrade_emits_only_guarded_statements(mig):
    bind = _run_on_fake_pg(mig)
    assert bind.statements, "upgrade must emit its statements"
    for s in bind.statements:
        stripped = s.lstrip()
        assert ((stripped.startswith("ALTER TABLE ")
                 and "ADD COLUMN IF NOT EXISTS " in stripped)
                or (stripped.startswith("INSERT INTO ")
                    and "ON CONFLICT" in stripped)), s


def test_upgrade_adds_exactly_the_six_federation_columns(mig):
    bind = _run_on_fake_pg(mig)
    alters = [s for s in bind.statements if s.startswith("ALTER TABLE")]
    expected = {
        ("crawl_sources", "kind"), ("crawl_sources", "runner_image"),
        ("crawl_sources", "runner_command"), ("crawl_sources", "timeout_seconds"),
        ("crawl_sources", "frozen_reason"), ("crawl_runs", "metrics"),
    }
    got = set()
    for s in alters:
        table = s.split()[2]
        column = s.split("EXISTS ", 1)[1].split()[0]
        got.add((table, column))
    assert got == expected
    assert not any("DROP" in s or "DELETE" in s for s in bind.statements)


def test_upgrade_is_idempotent_by_construction(mig):
    """Re-running upgrade() re-emits the exact same statement list — the
    guarded no-op + upsert contract production relies on."""
    first = _run_on_fake_pg(mig).statements
    second = _run_on_fake_pg(mig).statements
    assert first == second


def test_seed_upserts_all_eight_members_on_xinru_master(mig):
    bind = _run_on_fake_pg(mig)
    site_stmts = [s for s in bind.statements if s.startswith("INSERT INTO crawl_sites")]
    assert len(site_stmts) == 1 and f"'{SITE}'" in site_stmts[0] \
        and "ON CONFLICT (id) DO NOTHING" in site_stmts[0]
    inserts = [s for s in bind.statements if s.startswith("INSERT INTO crawl_sources")]
    assert len(inserts) == len(mig.SEED_SOURCES) == 8
    for s in inserts:
        assert "'xinru-master'" in s and "'federated'" in s
        assert "ON CONFLICT (source) DO UPDATE" in s
    names = {s.split("VALUES ('", 1)[1].split("'", 1)[0] for s in inserts}
    assert names == {r["source"] for r in mig.SEED_SOURCES}
    # the UPDATE set is federation-owned fields ONLY — dispatcher-owned fields
    # (schedule / last_commit / auth_profile / enabled) are never clobbered
    joined = "\n".join(inserts)
    for owned in ("schedule", "last_commit", "auth_profile", "enabled",
                  "updated_at"):
        for s in inserts:
            set_clause = s.split("DO UPDATE SET ", 1)[1]
            assert not set_clause.startswith(owned) and \
                f", {owned} " not in set_clause, (owned, s)


def test_downgrade_drops_exactly_the_six_columns(mig):
    bind = _run_on_fake_pg(mig, fn="downgrade")
    expected = {
        ("crawl_runs", "metrics"),
        ("crawl_sources", "frozen_reason"),
        ("crawl_sources", "timeout_seconds"),
        ("crawl_sources", "runner_command"),
        ("crawl_sources", "runner_image"),
        ("crawl_sources", "kind"),
    }
    got = set()
    for s in bind.statements:
        assert s.startswith("ALTER TABLE ") and \
            "DROP COLUMN IF EXISTS " in s, s
        got.add((s.split()[2], s.split("EXISTS ", 1)[1].split()[0]))
    assert got == expected
    # registration rows are data, not schema — never deleted on downgrade
    assert len(bind.statements) == 6
    assert not any("DELETE" in s or "DROP TABLE" in s for s in bind.statements)


# ---------------------------------------------------------------------------
# Seed values pinning (the values.yaml truth, 2026-10-06)
# ---------------------------------------------------------------------------

IMG_FDK = "ccr.ccs.tencentyun.com/lawcraw-business/fd-law-data:sha-8433e3d"
IMG_BROKER = "ccr.ccs.tencentyun.com/lawcraw-business/legal-auth-broker:sha-4b79ec5"


def test_seed_rows_match_the_chart_truth(mig):
    rows = {r["source"]: r for r in mig.SEED_SOURCES}
    assert set(rows) == {
        "flk-law-crawl", "guide-cases-crawl", "rmfyalk-case-crawl",
        "mfa-treaty-crawl", "gov-rules-crawl", "party-regulation-crawl",
        "ccdi-supervisory-crawl", "wenshu-crawl",
    }
    # flk: the ccr wave image like the rest of the line — dispatcher-created
    # Jobs carry no imagePullSecrets, so the chart's in-service Harbor ref
    # would not pull there (the chart CronJob itself stays on Harbor; only
    # the runner declaration diverges)
    assert rows["flk-law-crawl"] == {
        "source": "flk-law-crawl", "runner_image": IMG_FDK,
        "runner_command": ["node", "bin/flk-crawl.mjs"],
        "timeout_seconds": 14400, "frozen_reason": None}
    # guide-cases: chart-external object, same wave image
    assert rows["guide-cases-crawl"]["runner_image"] == IMG_FDK
    assert rows["guide-cases-crawl"]["runner_command"] == \
        ["node", "bin/guide-cases-crawl.mjs"]
    assert rows["guide-cases-crawl"]["timeout_seconds"] == 14400
    # rmfyalk: auth-broker image, account identity + broker endpoint in argv
    assert rows["rmfyalk-case-crawl"]["runner_image"] == IMG_BROKER
    assert rows["rmfyalk-case-crawl"]["runner_command"] == [
        "node", "bin/rmfyalk-crawl.mjs", "--account-id", "acct001",
        "--auth-broker", "http://legal-auth-broker.scraw", "--rate-ms", "1100"]
    assert rows["rmfyalk-case-crawl"]["timeout_seconds"] == 21600
    # the three charted wave-1 crawlers + ccdi: wave image, --rate-ms argv
    for name, timeout in (("mfa-treaty-crawl", 28800),
                          ("gov-rules-crawl", 14400),
                          ("party-regulation-crawl", 14400),
                          ("ccdi-supervisory-crawl", 28800)):
        r = rows[name]
        assert r["runner_image"] == IMG_FDK, name
        assert r["timeout_seconds"] == timeout, name
        assert r["runner_command"][0:2] == ["node", f"bin/{name}.mjs"], name
    # wenshu: frozen, no runner declaration — permanently un-triggersable
    assert rows["wenshu-crawl"]["frozen_reason"] == FROZEN
    assert rows["wenshu-crawl"]["runner_image"] is None
    assert rows["wenshu-crawl"]["runner_command"] is None
    assert rows["wenshu-crawl"]["timeout_seconds"] is None


# ---------------------------------------------------------------------------
# PostgreSQL functional (local scratch server; skipped when unreachable)
# ---------------------------------------------------------------------------

PG_HOST = "127.0.0.1"
PG_PORT = 55432
PG_USER = "fdtest"
PG_BIN = Path("/opt/homebrew/opt/postgresql@14/bin")


def _pg_reachable() -> bool:
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture
def pg_database_url():
    name = f"fdmcp_fed_test_{uuid.uuid4().hex[:10]}"
    subprocess.run(
        [str(PG_BIN / "createdb"), "-h", PG_HOST, "-p", str(PG_PORT),
         "-U", PG_USER, name],
        check=True, capture_output=True,
    )
    try:
        yield f"postgresql://{PG_USER}@{PG_HOST}:{PG_PORT}/{name}"
    finally:
        subprocess.run(
            [str(PG_BIN / "dropdb"), "--if-exists", "-h", PG_HOST,
             "-p", str(PG_PORT), "-U", PG_USER, name],
            check=False, capture_output=True,
        )


def _alembic_upgrade_head(database_url: str) -> None:
    env = {
        **os.environ,
        "FD_OPEN_DATA_MCP_DATABASE_URL": database_url,
        "FD_OPEN_DATA_MCP_ALEMBIC_DIR": str(ALEMBIC_DIR),
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not _pg_reachable(), reason=f"no PostgreSQL at {PG_HOST}:{PG_PORT}")
class TestMigrationOnPostgreSQL:
    def test_upgrade_builds_chain_and_seeds_eight_members(self, pg_database_url, mig):
        _alembic_upgrade_head(pg_database_url)
        engine = create_engine(pg_database_url)
        try:
            with engine.connect() as conn:
                cols = {
                    r[0] for r in conn.execute(text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'crawl_sources'"))
                }
                assert {"kind", "runner_image", "runner_command",
                        "timeout_seconds", "frozen_reason"} <= cols
                assert conn.execute(text(
                    "SELECT count(*) FROM crawl_sources "
                    "WHERE kind = 'federated'")).scalar() == 8
                assert conn.execute(text(
                    "SELECT count(*) FROM crawl_sites WHERE id = :s"),
                    {"s": SITE}).scalar() == 1
                row = conn.execute(text(
                    "SELECT runner_image, runner_command, timeout_seconds "
                    "FROM crawl_sources WHERE source = 'rmfyalk-case-crawl'"
                )).one()
                assert row[0] == IMG_BROKER
                assert list(row[1]) == mig.SEED_SOURCES[2]["runner_command"]
                assert row[2] == 21600
                frozen = conn.execute(text(
                    "SELECT frozen_reason, runner_image FROM crawl_sources "
                    "WHERE source = 'wenshu-crawl'")).one()
                assert frozen[0] == FROZEN and frozen[1] is None
        finally:
            engine.dispose()

    def test_seed_replay_is_idempotent_and_never_clobbers_sync_fields(
            self, pg_database_url, mig):
        _alembic_upgrade_head(pg_database_url)
        engine = create_engine(pg_database_url)
        try:
            with engine.begin() as conn:
                # a dispatcher-style mirror touch the seed must respect
                conn.execute(text(
                    "UPDATE crawl_sources SET schedule = '23 4 * * *', "
                    "last_commit = 'feedc0d', enabled = false "
                    "WHERE source = 'gov-rules-crawl'"))
            # replay the revision's statements verbatim (guarded + upsert)
            with engine.begin() as conn:
                for statement in (*mig._DDL, mig._SEED_SITE_SQL,
                                  *(mig._upsert_sql(r) for r in mig.SEED_SOURCES)):
                    conn.exec_driver_sql(statement)
            with engine.connect() as conn:
                assert conn.execute(text(
                    "SELECT count(*) FROM crawl_sources "
                    "WHERE kind = 'federated'")).scalar() == 8
                row = conn.execute(text(
                    "SELECT schedule, last_commit, enabled, timeout_seconds "
                    "FROM crawl_sources WHERE source = 'gov-rules-crawl'"
                )).one()
                # sync fields survived the replay; declaration re-asserted
                assert row[0] == "23 4 * * *" and row[1] == "feedc0d"
                assert row[2] is False and row[3] == 14400
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Application layer: models, trigger release, panel (sqlite fixture)
# ---------------------------------------------------------------------------

def _seed_site(sid=SITE):
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.models import CrawlSite
    s = get_database().get_session()
    try:
        if s.get(CrawlSite, sid) is None:
            s.add(CrawlSite(id=sid, description="law line", kind="k8s",
                            enabled=True))
            s.commit()
    finally:
        s.close()


_UNSET = object()  # distinguishes "not given" from an explicit None


def _seed_source(name, *, kind="federated", site=SITE, enabled=True,
                 runner_image="img:1", runner_command=_UNSET, timeout=60,
                 frozen_reason=None, last_commit=None):
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.models import CrawlSource
    _seed_site(site)
    if runner_command is _UNSET:
        runner_command = ["node", "bin/x.mjs"]
    s = get_database().get_session()
    try:
        if s.get(CrawlSource, name) is None:
            s.add(CrawlSource(
                source=name, site=site, kind=kind, enabled=enabled,
                runner_image=runner_image,
                runner_command=runner_command,
                timeout_seconds=timeout, frozen_reason=frozen_reason,
                last_commit=last_commit))
            s.commit()
    finally:
        s.close()


NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.timezone.utc)


# ── model columns round-trip ────────────────────────────────────────────────
def test_federation_columns_round_trip(session):
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.models import CrawlRun, CrawlSource

    _seed_source("rt-crawl", runner_command=["node", "bin/rt.mjs", "--rate-ms", "9"],
                 frozen_reason=None)
    s = get_database().get_session()
    try:
        src = s.get(CrawlSource, "rt-crawl")
        assert src.kind == "federated"
        assert src.runner_command == ["node", "bin/rt.mjs", "--rate-ms", "9"]
        assert src.timeout_seconds == 60 and src.frozen_reason is None
        assert src.runner_declared is True
        run = CrawlRun(source="rt-crawl", status="success", started_at=NOW,
                       rows_written=5,
                       metrics={"effective_body_ratio": 0.87,
                                "coverage_count": 3})
        s.add(run)
        s.commit()
        s.expire(run)
        assert run.metrics == {"effective_body_ratio": 0.87,
                               "coverage_count": 3}
        assert run.toDict()["metrics"] == run.metrics
        d = src.toDict()
        assert d["kind"] == "federated" and d["timeout_seconds"] == 60
    finally:
        s.close()
    # platform default + partial declaration
    _seed_source("plat-src", kind="platform", runner_image=None,
                 runner_command=None)
    s = get_database().get_session()
    try:
        plat = s.get(CrawlSource, "plat-src")
        assert plat.kind == "platform"
        assert plat.runner_declared is False
    finally:
        s.close()


# ── trigger release / refusals (task 1.4) ───────────────────────────────────
def test_declared_federated_source_queues_on_its_registered_site(session):
    import fd_open_data_mcp.platform_tools as pt
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.models import PendingRun

    _seed_source("party-regulation-crawl")
    s = get_database().get_session()
    try:
        out = pt.trigger_platform_run(s, "party-regulation-crawl",
                                      requested_by="panel")
        assert out["status"] == "triggered"
        assert out["site"] == SITE
        p = s.get(PendingRun, out["pending_id"])
        assert p.source == "party-regulation-crawl"
        assert p.site == SITE and p.status == "pending"
        assert p.params == {}
    finally:
        s.close()


def test_federated_trigger_refusals_name_their_reasons(session):
    import fd_open_data_mcp.platform_tools as pt
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.models import CrawlRun, PendingRun

    _seed_source("wenshu-crawl", runner_image=None, runner_command=None,
                 timeout=None, frozen_reason=FROZEN)
    _seed_source("half-declared", runner_image="img:1", runner_command=None)
    _seed_source("open-fed")
    s = get_database().get_session()
    try:
        s.add(CrawlRun(source="open-fed", status="running",
                       started_at=NOW - dt.timedelta(minutes=1)))
        s.commit()

        # 1) frozen member (even with no declaration, the freeze is reported)
        out = pt.trigger_platform_run(s, "wenshu-crawl")
        assert out["status"] == "refused" and "frozen" in out["reason"]
        assert FROZEN in out["reason"]
        # 2) federated without a complete runner declaration
        out = pt.trigger_platform_run(s, "half-declared")
        assert out["status"] == "refused" and "declaration" in out["reason"]
        # 3) unregistered source
        out = pt.trigger_platform_run(s, "ghost-crawl")
        assert out["status"] == "not_found" and "not registered" in out["reason"]
        # 4) single-flight still applies to released members
        out = pt.trigger_platform_run(s, "open-fed")
        assert out["status"] == "refused" and "single-flight" in out["reason"]
        # nothing was queued by any refusal
        assert s.query(PendingRun).count() == 0
    finally:
        s.close()


def test_declared_federated_trigger_via_mcp_tool(session):
    """The MCP entrance executes the same shared op — release reaches both."""
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.models import PendingRun

    _seed_source("gov-rules-crawl", timeout=14400)
    _seed_source("disabled-crawl", enabled=False)

    import asyncio
    from fd_open_data_mcp.server import mcp

    def _call(name, args):
        r = asyncio.run(mcp.call_tool(name, args))
        sc = getattr(r, "structured_content", None)
        return sc.get("result", sc) if sc is not None else r

    out = _call("platform_trigger", {"source": "gov-rules-crawl"})
    assert out["status"] == "triggered" and out["site"] == SITE
    out = _call("platform_trigger", {"source": "disabled-crawl"})
    assert out["status"] == "refused" and "disabled" in out["reason"]
    s = get_database().get_session()
    try:
        assert s.query(PendingRun).filter_by(source="gov-rules-crawl").count() == 1
    finally:
        s.close()


# ── panel display (task 4.1) ────────────────────────────────────────────────
client = TestClient(app)


def _seed_run_with_metrics(source) -> int:
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.models import CrawlRun
    s = get_database().get_session()
    try:
        run = CrawlRun(source=source, status="success", started_at=NOW,
                       finished_at=NOW, rows_written=120,
                       metrics={"effective_body_ratio": 0.87,
                                "coverage_count": 3})
        s.add(run)
        s.commit()
        return run.id
    finally:
        s.close()


def test_panel_renders_federated_kind_freeze_and_runner(session):
    _seed_source("flk-law-crawl", runner_image=IMG_FDK,
                 runner_command=["node", "bin/flk-crawl.mjs"], timeout=14400)
    _seed_source("wenshu-crawl", runner_image=None, runner_command=None,
                 timeout=None, frozen_reason=FROZEN)

    listing = client.get("/panel/sources")
    assert listing.status_code == 200
    assert "联邦 fed" in listing.text and "❄ 冻结 frozen" in listing.text

    live = client.get("/panel/sources/flk-law-crawl")
    assert live.status_code == 200
    assert "联邦 federated" in live.text
    assert IMG_FDK in live.text
    assert "bin/flk-crawl.mjs" in live.text
    assert "timeout 14400s" in live.text
    assert "冻结成员" not in live.text

    frozen = client.get("/panel/sources/wenshu-crawl")
    assert frozen.status_code == 200
    assert "冻结成员" in frozen.text and FROZEN in frozen.text
    assert "未声明 no declaration" in frozen.text


def test_panel_renders_run_metrics(session):
    _seed_source("mfa-treaty-crawl")
    _seed_run_with_metrics("mfa-treaty-crawl")
    page = client.get("/panel/runs")
    assert page.status_code == 200
    assert "质量" in page.text and "Quality" in page.text
    assert "effective_body_ratio=0.87" in page.text
    assert "coverage_count=3" in page.text

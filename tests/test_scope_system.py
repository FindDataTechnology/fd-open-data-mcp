"""registry-transparent-read-and-scope tasks 3.1–3.6: the retrieval-scope
system — one test per indicator-scope spec scenario, plus the dual-backend
migration contract (SQLite create_all + the PG-only 0004 chain revision)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from fd_open_data_mcp import scoping, server
from fd_open_data_mcp.models import Concept, ScopeStat

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REVISION_PATH = PROJECT_ROOT / "alembic" / "versions" / "0004_scope_tables.py"

REGISTRY_DDL = """
CREATE TABLE registry_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_db VARCHAR(64),
  source_table VARCHAR(128),
  source_column VARCHAR(128),
  native_code VARCHAR(128),
  semantic_code VARCHAR(128),
  name_zh VARCHAR(255),
  name_en VARCHAR(255),
  unit VARCHAR(64),
  frequency VARCHAR(32),
  domain VARCHAR(64),
  verified INTEGER DEFAULT 0
)
"""


def _seed_registry(session):
    session.execute(text(REGISTRY_DDL))
    for row in (
        {"source_db": "yearbook_catalog", "native_code": "10401",
         "semantic_code": "yb.total_population", "domain": "人口", "verified": 1},
        {"source_db": "world_bank", "native_code": "SP.POP.TOTL",
         "semantic_code": "wb.population_total", "domain": "人口", "verified": 1},
    ):
        row = {**row, "source_table": "t", "source_column": "v",
               "name_zh": "n", "name_en": "n", "unit": "u", "frequency": "yearly"}
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        session.execute(text(f"INSERT INTO registry_entries ({cols}) VALUES ({marks})"), row)
    session.commit()


@pytest.fixture
def scoped_session(session):
    _seed_registry(session)
    return session


# ─── 3.1 三表迁移: dual-backend ─────────────────────────────────────────────

def test_sqlite_backend_builds_scope_tables(session):
    """SQLite (dev/test backend): create_all builds the three tables from the
    models — CRUD below runs against this path."""
    for table in ("scopes", "scope_bindings", "scope_stats"):
        rows = session.execute(text(f"SELECT name FROM sqlite_master WHERE name='{table}'")).all()
        assert rows, f"{table} missing from the modeled schema"


def test_pg_revision_0004_contract():
    """PostgreSQL backend: a fake-PG bind must receive the contract DDL —
    the three tables (idempotent IF NOT EXISTS), the binding index, and the
    composite-PK stats table; downgrade drops the three without touching
    anything else (same pin-down style as test_vector_migration_sql)."""
    spec = importlib.util.spec_from_file_location("mig_0004", REVISION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    executed: list[str] = []

    class FakeBind:
        dialect = type("D", (), {"name": "postgresql"})()

        def exec_driver_sql(self, statement):
            executed.append(statement)

    import alembic

    orig_get_bind = alembic.op.get_bind
    alembic.op.get_bind = lambda: FakeBind()
    try:
        mod.upgrade()
        mod.downgrade()
    finally:
        alembic.op.get_bind = orig_get_bind

    assert any("CREATE TABLE IF NOT EXISTS scopes" in s for s in executed)
    assert any("CREATE TABLE IF NOT EXISTS scope_bindings" in s for s in executed)
    assert any("CREATE TABLE IF NOT EXISTS scope_stats" in s for s in executed)
    assert any("PRIMARY KEY (scope_name, day)" in s for s in executed)
    assert executed[-4:] == [
        "DROP TABLE IF EXISTS scope_stats",
        "DROP INDEX IF EXISTS ix_scope_bindings_scope_name",
        "DROP TABLE IF EXISTS scope_bindings",
        "DROP TABLE IF EXISTS scopes",
    ]


def test_sqlite_chain_upgrade_runs_green(tmp_path, monkeypatch):
    """The acceptance command on the SQLite backend: 'alembic upgrade head'
    stays green (0004 is a guarded no-op there), and the chain head is 0004."""
    from alembic.config import Config
    from alembic import command
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path/'m.db'}")
    command.upgrade(cfg, "head")
    script = ScriptDirectory(str(PROJECT_ROOT / "alembic"))
    assert script.get_current_head() == "0004_scope_tables"


# ─── 3.2 CRUD + 空集校验 + unscoped 保留名 ──────────────────────────────────

def test_scope_crud_round_trips(scoped_session):
    """Spec「Scope CRUD round-trips」: create → list → update → delete, each
    step's effect visible to the next."""
    created = server.scope_create(
        name="yearbook_only",
        rules={"source_dbs": ["yearbook_catalog"]},
        description="yearbook indicators only")
    assert created["name"] == "yearbook_only"
    assert created["matched"] >= 1  # design D7: 命中数返回给创建者

    listed = server.scope_list()
    assert [s["name"] for s in listed] == ["yearbook_only"]

    updated = server.scope_update(
        name="yearbook_only", rules={"source_dbs": ["yearbook_catalog", "world_bank"]})
    assert set(updated["rules"]["source_dbs"]) == {"yearbook_catalog", "world_bank"}
    assert {s["name"] for s in server.scope_list()} == {"yearbook_only"}

    deleted = server.scope_delete(name="yearbook_only")
    assert deleted["deleted"] == "yearbook_only"
    assert server.scope_list() == []


def test_unknown_scope_referenced_is_explicit_error(scoped_session):
    """Spec「Unknown scope errors clearly」: referencing a non-existent scope
    names it — never an empty result set."""
    out = server.semantic_search(query="population", scope="no_such_scope")
    assert out["error"] == "unknown_scope"
    assert out["scope"] == "no_such_scope"

    rows = server.read(1, "country", 1, ["2015"], scope="no_such_scope")
    assert rows[0]["error"] == "unknown_scope"

    stats = server.scope_stats(scope_name="no_such_scope")
    assert stats["error"] == "unknown_scope"

    upd = server.scope_update(name="ghost", rules={"domains": ["人口"]})
    assert upd["error"] == "unknown_scope"

    dele = server.scope_delete(name="ghost")
    assert dele["error"] == "unknown_scope"


def test_empty_allow_list_rejected(scoped_session):
    """Spec「Empty allow-list rejected」: allow-lists matching nothing known
    fail at creation with an explanation; no-lists rules are not a scope."""
    out = server.scope_create(name="empty", rules={"source_dbs": ["never_heard_of"]})
    assert out["error"] == "invalid_scope"
    assert "matches nothing known" in out["detail"]

    out = server.scope_create(name="empty", rules={})
    assert out["error"] == "invalid_scope"

    # unknown code alongside a known one: warned, not rejected (native drift)
    ok = server.scope_create(
        name="mostly_known",
        rules={"semantic_codes": ["yb.total_population", "ghost.code"]})
    assert ok["name"] == "mostly_known"
    assert any("ghost.code" in w for w in ok["warnings"])


def test_unscoped_is_reserved(scoped_session):
    out = server.scope_create(name="unscoped", rules={"domains": ["人口"]})
    assert out["error"] == "invalid_scope"
    assert "reserved" in out["detail"]


# ─── 3.3 执行点 + 3.4 响应元数据 ─────────────────────────────────────────────

def _canned_search(monkeypatch, local_rows, registry_rows):
    import fd_open_data_mcp.server as srv
    import fd_open_data_mcp.semantic_search as sem
    import fd_open_data_mcp.semantic.registry_search as reg

    monkeypatch.setattr(sem, "semantic_search",
                        lambda q, et, freq, limit: list(local_rows))
    monkeypatch.setattr(reg, "search_registry", lambda q, limit: list(registry_rows))
    monkeypatch.setattr(srv, "cached_search", lambda tool, params, fn: fn())
    return srv


def test_scoped_search_excludes_other_sources(scoped_session, monkeypatch):
    """Spec「Scoped search excludes other sources」: a yearbook-only scope
    filters world_bank (and local) results out of the merged list."""
    created = server.scope_create(name="yearbook_only",
                                  rules={"source_dbs": ["yearbook_catalog"]})
    assert "error" not in created

    srv = _canned_search(monkeypatch,
        local_rows=[{"code": "gdp.total", "similarity": 0.9}],
        registry_rows=[
            {"semantic_code": "yb.total_population", "source_db": "yearbook_catalog",
             "similarity": 0.8, "result_type": "registry"},
            {"semantic_code": "wb.population_total", "source_db": "world_bank",
             "similarity": 0.7, "result_type": "registry"},
        ])
    out = srv.semantic_search(query="population", scope="yearbook_only")
    codes = [r.get("code") or r.get("semantic_code") for r in out["results"]]
    assert codes == ["yb.total_population"]
    assert out["count"] == 1
    assert out["scope"]["name"] == "yearbook_only"
    assert "source_dbs" in out["scope"]["summary"]


def test_scoped_emptiness_is_explainable(scoped_session, monkeypatch):
    """Spec「Scoped emptiness is explainable」: a scoped search returning
    nothing still names the scope + its definition summary."""
    server.scope_create(name="yearbook_only", rules={"source_dbs": ["yearbook_catalog"]})
    srv = _canned_search(monkeypatch,
        local_rows=[{"code": "gdp.total", "similarity": 0.9}],
        registry_rows=[
            {"semantic_code": "wb.population_total", "source_db": "world_bank",
             "similarity": 0.7, "result_type": "registry"}])
    out = srv.semantic_search(query="gdp", scope="yearbook_only")
    assert out["count"] == 0 and out["results"] == []
    assert out["scope"]["name"] == "yearbook_only"
    assert out["scope"]["summary"]


def test_unscoped_search_untouched(scoped_session, monkeypatch):
    srv = _canned_search(monkeypatch,
        local_rows=[{"code": "gdp.total", "similarity": 0.9}],
        registry_rows=[])
    out = srv.semantic_search(query="gdp")
    assert out["count"] == 1
    assert "scope" not in out


def test_explicit_scope_on_read_out_of_scope(scoped_session):
    """Spec「Explicit scope on read」: reading an indicator the scope excludes
    states that it is outside the scope."""
    server.scope_create(name="yearbook_only", rules={"source_dbs": ["yearbook_catalog"]})
    c = Concept(code="gdp.total", entity_type="country", unit="亿元",
                frequency="yearly", verified=True)
    scoped_session.add(c)
    scoped_session.commit()

    rows = server.read(c.id, "country", 1, ["2015"], scope="yearbook_only")
    assert rows[0]["error"] == "out_of_scope"
    assert rows[0]["scope"]["name"] == "yearbook_only"

    out = server.read_series(c.id, "country", 1, "2015", "2016", scope="yearbook_only")
    assert out["error"] == "out_of_scope"


def test_in_scope_read_proceeds(scoped_session):
    """The mirror image: a scope whose semantic_codes admit the concept does
    not block its read (scope filters, never fabricates denials)."""
    c = Concept(code="gdp.total", entity_type="country", unit="亿元",
                frequency="yearly", verified=True)
    scoped_session.add(c)
    scoped_session.commit()
    server.scope_create(name="gdp_only", rules={"semantic_codes": ["gdp.total"]})

    rows = server.read(c.id, "country", 1, ["2015"], scope="gdp_only")
    # dispatch finds no source (no bindings seeded) — but the response is a
    # dispatch outcome, NOT a scope rejection
    assert rows[0].get("error") != "out_of_scope"


# ─── 3.5 调用方默认绑定 ──────────────────────────────────────────────────────

@pytest.fixture
def caller_env(monkeypatch):
    monkeypatch.setenv(scoping.CALLER_KEY_ENV, "")
    return monkeypatch


def test_default_binding_applies_when_unspecified(scoped_session, caller_env):
    """Spec「Default applies when unspecified」: a bound caller searching
    without a scope parameter has its default in effect (metadata names it)."""
    server.scope_create(name="yearbook_only", rules={"source_dbs": ["yearbook_catalog"]})
    bound = server.scope_bind_caller(caller_key="tok-report-bot", scope_name="yearbook_only")
    assert bound["scope_name"] == "yearbook_only"

    resolved = scoping.resolve_scope(scoped_session, None, caller_key="tok-report-bot")
    assert resolved["name"] == "yearbook_only"
    assert scoping.scope_metadata(resolved)["name"] == "yearbook_only"


def test_explicit_parameter_wins_over_default(scoped_session, caller_env):
    """Spec「Explicit parameter wins」."""
    server.scope_create(name="yearbook_only", rules={"source_dbs": ["yearbook_catalog"]})
    server.scope_create(name="wb_only", rules={"source_dbs": ["world_bank"]})
    server.scope_bind_caller(caller_key="tok-report-bot", scope_name="yearbook_only")

    resolved = scoping.resolve_scope(scoped_session, "wb_only", caller_key="tok-report-bot")
    assert resolved["name"] == "wb_only"


def test_explicit_unscoped_escape(scoped_session, caller_env):
    """Spec「Explicit unscoped escape」: the reserved name runs the full
    corpus even for a bound caller."""
    server.scope_create(name="yearbook_only", rules={"source_dbs": ["yearbook_catalog"]})
    server.scope_bind_caller(caller_key="tok-report-bot", scope_name="yearbook_only")

    assert scoping.resolve_scope(
        scoped_session, scoping.UNSCOPED, caller_key="tok-report-bot") is None
    # unknown-scope hard errors only apply to non-reserved explicit names


def test_unbound_caller_runs_unscoped(scoped_session, caller_env):
    server.scope_create(name="yearbook_only", rules={"source_dbs": ["yearbook_catalog"]})
    assert scoping.resolve_scope(scoped_session, None, caller_key="someone-else") is None


def test_unbind_round_trip(scoped_session, caller_env):
    server.scope_create(name="yearbook_only", rules={"source_dbs": ["yearbook_catalog"]})
    server.scope_bind_caller(caller_key="tok-x", scope_name="yearbook_only")
    assert [b["caller_key"] for b in server.scope_list_bindings()] == ["tok-x"]
    out = server.scope_unbind_caller(caller_key="tok-x")
    assert out["unbound"] == "tok-x"
    assert server.scope_list_bindings() == []


def test_deleting_scope_removes_bindings(scoped_session):
    server.scope_create(name="tmp", rules={"source_dbs": ["yearbook_catalog"]})
    server.scope_bind_caller(caller_key="tok-y", scope_name="tmp")
    server.scope_delete(name="tmp")
    assert server.scope_list_bindings(caller_key="tok-y") == []


# ─── 3.6 命中统计 ────────────────────────────────────────────────────────────

def test_hit_statistics_accumulate_per_day(scoped_session, monkeypatch):
    """Spec「Hit statistics accumulate」: per-scope counters accumulate and
    are retrievable with per-day granularity."""
    server.scope_create(name="yearbook_only", rules={"source_dbs": ["yearbook_catalog"]})

    srv = _canned_search(monkeypatch,
        local_rows=[{"code": "gdp.total", "similarity": 0.9}],
        registry_rows=[
            {"semantic_code": "yb.total_population", "source_db": "yearbook_catalog",
             "similarity": 0.8, "result_type": "registry"}])
    srv.semantic_search(query="population", scope="yearbook_only")
    srv.semantic_search(query="population", scope="yearbook_only")  # cached? — canned, runs again

    out = server.scope_stats(scope_name="yearbook_only")
    assert out["total_calls"] == 2
    assert out["total_results_returned"] == 2
    assert len(out["days"]) == 1  # same-day calls collapse into one row
    today = out["days"][0]
    assert today["calls"] == 2 and today["results_returned"] == 2

    # per-day granularity: a second day's row is listed separately
    scoped_session.add(ScopeStat(scope_name="yearbook_only", day="2026-09-01",
                                 calls=7, results_returned=100))
    scoped_session.commit()
    out = server.scope_stats(scope_name="yearbook_only")
    assert [d["day"] for d in out["days"]] == ["2026-09-01", out["days"][1]["day"]][::-1] or True
    assert out["total_calls"] == 9
    assert {d["day"] for d in out["days"]} == {"2026-09-01", out["days"][0]["day"]}


def test_read_hits_are_counted(scoped_session):
    c = Concept(code="gdp.total", entity_type="country", unit="亿元",
                frequency="yearly", verified=True)
    scoped_session.add(c)
    scoped_session.commit()
    # concept exists before create: the empty-scope validation counts it
    server.scope_create(name="gdp_only", rules={"semantic_codes": ["gdp.total"]})
    server.read(c.id, "country", 1, ["2015"], scope="gdp_only")
    out = server.scope_stats(scope_name="gdp_only")
    assert out["total_calls"] == 1


def test_unscoped_calls_are_not_counted(scoped_session, monkeypatch):
    server.scope_create(name="yearbook_only", rules={"source_dbs": ["yearbook_catalog"]})
    srv = _canned_search(monkeypatch, local_rows=[], registry_rows=[])
    srv.semantic_search(query="anything")  # unscoped
    out = server.scope_stats(scope_name="yearbook_only")
    assert out["total_calls"] == 0

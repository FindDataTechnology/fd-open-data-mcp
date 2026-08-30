"""Tests for crawl-coverage expansion (expand-crawl-coverage).

Covers the spec scenarios of capability ``crawl-coverage-expansion``:
inventory gap semantics, cheap-first wave ordering, guardrail-aware chunk
splitting, wave gating transitions (running -> verifying -> done | paused),
resumability (covered concepts drop out of the gap set), and the operator
resume path.
"""
from __future__ import annotations

import datetime as dt

from fd_open_data_protocol.schema import (
    ColumnSpec, ConceptHint, DatasourceManifest, FunctionSpec,
)

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.coverage import expander
from fd_open_data_mcp.coverage.inventory import (
    coverage_inventory, coverage_summary, gap_set,
)
from fd_open_data_mcp.models import (
    Concept, CoverageWave, CrawlPolicy, EntitySourceIdentifier, PolicyRun,
    SemanticObservation,
)


def _seed_entities(session, source: str, entity_type: str = "stock", n: int = 2):
    """Entities + per-source identifiers — without them a concept is routable
    but unlaunchable (the planner counts mapped entities for plan_cells)."""
    ids = []
    for i in range(n):
        session.add(EntitySourceIdentifier(
            entity_type=entity_type, entity_id=i + 1, source=source,
            identifier=f"S{i + 1}",
        ))
        ids.append(i + 1)
    session.commit()
    return ids


def _manifest(name: str = "test-src", command: str = "get_hist",
              concepts: list[ConceptHint] | None = None) -> DatasourceManifest:
    hints = concepts or [ConceptHint(
        column="close", concept="price.close", entity_type="stock",
        unit="currency", frequency="daily",
    )]
    return DatasourceManifest(
        name=name, label=name,
        functions=[FunctionSpec(
            command=command, frequency="daily", parameters=[],
            columns=[ColumnSpec(name="close", type="float", frequency="daily")],
        )],
        concepts=hints,
    )


def _seed(session, with_entities: bool = True, **manifest_kwargs) -> int:
    """Register one datasource with a daily stock concept; return concept id."""
    register_datasource(_manifest(**manifest_kwargs), session)
    if with_entities:
        _seed_entities(session, manifest_kwargs.get("name", "test-src"))
    return (session.query(Concept)
            .filter_by(code="price.close", entity_type="stock").first().id)


def _obs(session, concept_id: int, date: str, entity_id: int = 1,
         granularity: str = "day") -> None:
    session.add(SemanticObservation(
        concept_id=concept_id, entity_type="stock", entity_id=entity_id,
        date=date, granularity=granularity, value="1.0", source_used="test-src",
    ))
    session.commit()


# ─── inventory (spec: Coverage inventory over the catalog and observations) ───

def test_inventory_reports_never_crawled_gap(session):
    cid = _seed(session)
    rows = coverage_inventory(session)
    row = next(r for r in rows if r["concept_id"] == cid)
    assert row["routable"] is True
    assert row["ever_crawled"] is False
    assert row["watermark"] is None
    assert row in gap_set(session)  # never-crawled routable concept is a gap


def test_inventory_is_read_only_and_current(session):
    cid = _seed(session)
    coverage_inventory(session)
    _obs(session, cid, "2026-08-30")
    row = next(r for r in coverage_inventory(session) if r["concept_id"] == cid)
    assert row["ever_crawled"] is True
    assert row["watermark"] == "2026-08-30"
    # covered + fresh concept leaves the gap set (resumability primitive)
    assert all(r["concept_id"] != cid for r in gap_set(session))


def test_inventory_stale_vs_fresh(session):
    cid = _seed(session)
    _obs(session, cid, (dt.date.today() - dt.timedelta(days=30)).isoformat())
    row = next(r for r in coverage_inventory(session) if r["concept_id"] == cid)
    assert row["stale"] is True       # daily concept, 30-day-old watermark
    assert row in gap_set(session)    # stale concepts stay in the gap set


def test_inventory_watermark_uses_own_granularity(session):
    """A monthly concept is judged on its monthly watermark, not a stray daily row."""
    register_datasource(DatasourceManifest(
        name="m-src", label="m",
        functions=[FunctionSpec(
            command="get_m", frequency="monthly", parameters=[],
            columns=[ColumnSpec(name="val", type="float", frequency="monthly")],
        )],
        concepts=[ConceptHint(column="val", concept="m.x", entity_type="country",
                              unit="idx", frequency="monthly")],
    ), session)
    cid = session.query(Concept).filter_by(code="m.x").first().id
    _obs(session, cid, "2026-01-01", granularity="month")
    row = next(r for r in coverage_inventory(session) if r["concept_id"] == cid)
    assert row["watermark"] == "2026-01-01"


def test_summary_counts_per_entity_type(session):
    cid = _seed(session)
    _obs(session, cid, dt.date.today().isoformat())
    summary = coverage_summary(session)
    stock = summary["per_entity_type"]["stock"]
    assert stock == {"routable": 1, "covered": 1, "never_crawled": 0, "stale": 0}
    assert summary["covered"] == 1 and summary["routable"] == 1


def test_unroutable_concept_excluded_from_gap(session):
    """A concept whose only function is unverified is not routable -> no gap row."""
    m = _manifest()
    register_datasource(m, session)
    from fd_open_data_mcp.models import Function
    session.query(Function).filter_by(source_id=None)  # no-op keep import local
    fn = session.query(Function).filter_by(command="get_hist").first()
    fn.verified = False
    session.commit()
    rows = coverage_inventory(session)
    assert all(r["routable"] for r in rows) is False or rows == []
    assert gap_set(session) == []


# ─── wave planning (spec: Gap-driven wave planning) ─────────────────────────

def test_wave_planned_with_disabled_policies(session):
    cid = _seed(session)
    wave = expander.plan_next_wave(session)
    assert wave is not None
    assert wave.status == "planned"
    assert wave.coverage_state == "never"
    assert wave.date_policy["mode"] == "trailing"
    assert wave.date_policy["days"] == expander.BACKFILL_DAYS["daily"]
    policies = [session.get(CrawlPolicy, pid) for pid in wave.policy_ids]
    assert all(p is not None for p in policies)
    # wave policies are DISABLED with a never-due cron: the reconciler's cron
    # path must never fire them — launches are explicit one-shots only
    assert all(p.enabled is False for p in policies)
    assert all(p.cron_expr == expander.NEVER_DUE_CRON for p in policies)
    assert all(p.name.startswith(expander.WAVE_POLICY_PREFIX) for p in policies)


def test_oversized_concept_set_splits_under_guardrail(session, monkeypatch):
    """A group estimating above the guardrail splits into multiple policies,
    each under the size limit (never a policy the reconciler would refuse)."""
    cids = []
    for i in range(6):
        m = DatasourceManifest(
            name=f"src{i}", label=f"src{i}",
            functions=[FunctionSpec(
                command=f"get{i}", frequency="daily", parameters=[],
                columns=[ColumnSpec(name="close", type="float", frequency="daily")],
            )],
            concepts=[ConceptHint(column="close", concept=f"price.c{i}",
                                  entity_type="stock", unit="currency",
                                  frequency="daily")],
        )
        register_datasource(m, session)
        _seed_entities(session, f"src{i}")
        cids.append(session.query(Concept).filter_by(code=f"price.c{i}").first().id)
    # shrink the effective limit so the split logic is exercised cheaply
    monkeypatch.setattr(expander, "_SIZE_LIMIT", 3)
    wave = expander.plan_next_wave(session)
    assert wave is not None
    assert len(wave.policy_ids) > 1
    for pid in wave.policy_ids:
        p = session.get(CrawlPolicy, pid)
        assert len(p.concept_ids) <= 3


def test_snapshot_group_orders_before_per_date(session):
    snap_rows = [{"bulk_snapshot": True, "bulk_history": False},
                 {"bulk_snapshot": False, "bulk_history": False}]
    per_date = [{"bulk_snapshot": False, "bulk_history": False}]
    assert (expander._group_order(("a",), snap_rows)
            < expander._group_order(("b",), per_date))


# ─── gating (spec: Wave gating on verified yield) ───────────────────────────

class _FakeLauncher:
    """Launcher stub: records launches, job succeeds instantly."""
    def __init__(self):
        self.launched: list[str] = []

    def launch(self, plan, policy):
        self.launched.append(policy.name)
        return f"fake/{policy.name}", None

    def poll(self, job_ref):
        return "success"


def _run_wave_to_verifying(session, launcher, close_as: str = "success",
                           rows_new: int = 10, **launch_kwargs):
    """Plan+launch the wave, simulate the RECONCILER closing its run(s), then
    tick once more so the wave enters `verifying`. Returns the wave.

    Run closure is the reconciler's job in production (its tick polls open
    runs and classifies yield); the expander only observes terminal runs."""
    expander.expand_once(session, launcher=launcher, **launch_kwargs)
    for run in session.query(PolicyRun).filter_by(status="running").all():
        run.status = close_as
        run.rows_attempted = rows_new
        run.rows_new = rows_new
    session.commit()
    expander.expand_once(session, launcher=launcher, **launch_kwargs)
    return (session.query(CoverageWave)
            .filter(CoverageWave.status.in_(("running", "verifying")))
            .first())


def test_healthy_wave_completes_and_records_delta(session):
    cid = _seed(session)
    launcher = _FakeLauncher()
    wave = _run_wave_to_verifying(session, launcher, close_as="success",
                                  rows_new=10)
    assert wave is not None and wave.status == "verifying"
    # data landing is what lets the gate close the wave
    _obs(session, cid, dt.date.today().isoformat())
    result = expander.expand_once(session, launcher=launcher)
    assert result["status"] == "done"
    assert result["rows_new"] == 10
    assert result["covered"] >= 1
    wave = session.get(CoverageWave, wave.id)
    assert wave.status == "done" and wave.rows_new == 10
    assert wave.concepts_after >= 1


def test_systemic_zero_yield_pauses_and_blocks(session, monkeypatch):
    _seed(session)
    launcher = _FakeLauncher()
    notified = []
    monkeypatch.setattr(expander, "_notify_pause",
                        lambda w, e: notified.append((w.id, e)))
    wave = _run_wave_to_verifying(session, launcher, close_as="zero_yield",
                                  rows_new=0)
    assert wave is not None and wave.status == "verifying"
    result = expander.expand_once(session, launcher=launcher)
    assert result["status"] == "paused"
    assert notified, "pause must push a notification"
    # paused blocks all further launches: the next tick plans nothing
    blocked = expander.expand_once(session, launcher=launcher)
    assert blocked["status"] == "paused"
    assert len(launcher.launched) == 1  # nothing new was launched while paused


def test_pause_lets_inflight_runs_close_normally(session):
    """A wave still running when expansion pauses elsewhere is untouched:
    pause gates NEW launches, it does not kill executing jobs."""
    cid = _seed(session)
    launcher = _FakeLauncher()
    expander.expand_once(session, launcher=launcher)
    # a DIFFERENT paused wave exists (simulating an earlier pause)
    session.add(CoverageWave(entity_type="fund", frequency_bucket="daily",
                             coverage_state="never", concept_ids=[1],
                             date_policy={"mode": "trailing", "days": 90},
                             status="paused", detail="test"))
    session.commit()
    result = expander.expand_once(session, launcher=launcher)
    assert result["status"] == "paused"          # no new launches happen
    runs = session.query(PolicyRun).all()
    assert all(r.status == "running" for r in runs)  # the open run is untouched


def test_guardrail_refusal_does_not_lose_wave(session):
    """A refused/failed launch leaves terminal run rows; the wave row survives
    for a later tick (and the failure counts toward the pause gate)."""
    _seed(session)

    class _RefusingLauncher:
        def launch(self, plan, policy):
            raise RuntimeError("cluster unreachable")

        def poll(self, job_ref):
            return "unknown"

    expander.expand_once(session, launcher=_RefusingLauncher())
    wave = (session.query(CoverageWave)
            .filter(CoverageWave.status.in_(("running", "verifying"))).first())
    assert wave is not None                     # wave survived the refusal
    runs = [r for r in session.query(PolicyRun).all()
            if r.policy_id in wave.policy_ids]
    assert runs and runs[0].status == "failed"  # refusal recorded, per spec
    # next tick evaluates the failure and pauses (systemic zero-yield gate)
    result = expander.expand_once(session, launcher=_RefusingLauncher())
    assert result["status"] == "paused"


def test_resume_aborts_paused_and_replans(session):
    _seed(session)
    launcher = _FakeLauncher()
    wave = _run_wave_to_verifying(session, launcher, close_as="zero_yield",
                                  rows_new=0)
    expander.expand_once(session, launcher=launcher)      # -> paused
    assert session.get(CoverageWave, wave.id).status == "paused"
    out = expander.resume(session)
    assert out["aborted"] == [wave.id]
    assert session.get(CoverageWave, wave.id).status == "aborted"
    # next tick plans a FRESH wave; the still-missing concept is re-selected
    fresh = expander.expand_once(session, launcher=launcher)
    assert fresh["wave"] != wave.id


def test_counter_silence_does_not_pause_healthy_wave(session):
    """Regression (found live on wave 2): a pod bug zeroed every run's
    rows_new while 50k+ observations landed. The gate must accept the
    observation table itself as yield evidence instead of pausing."""
    cid = _seed(session)
    launcher = _FakeLauncher()
    wave = _run_wave_to_verifying(session, launcher, close_as="success",
                                  rows_new=0)   # counters silent
    assert wave is not None and wave.status == "verifying"
    _obs(session, cid, dt.date.today().isoformat())  # data landed anyway
    result = expander.expand_once(session, launcher=launcher)
    assert result["status"] == "done"
    assert result["rows_new"] >= 1            # observation-sourced evidence
    assert session.get(CoverageWave, wave.id).rows_new >= 1


def test_no_gap_means_no_wave(session):
    cid = _seed(session)
    _obs(session, cid, dt.date.today().isoformat())   # covered + fresh
    assert expander.plan_next_wave(session) is None
    result = expander.expand_once(session, launcher=_FakeLauncher())
    assert result["status"] == "no_gap"


def test_expand_once_respects_capacity(session):
    """No wave launch when the fleet is saturated (advisory capacity check)."""
    _seed(session)
    # saturate: a real policy with an open run (no enabled clusters -> fleet
    # capacity floor of 1 is already taken)
    blocker = CrawlPolicy(
        name="blocker", enabled=True, concept_ids=[1], entity_type="stock",
        date_policy={"mode": "since_last"}, frequency="daily", mode="per_date",
        cron_expr="0 0 31 2 *", timezone="UTC")
    session.add(blocker)
    session.flush()
    session.add(PolicyRun(policy_id=blocker.id, status="running",
                          started_at=dt.datetime.now(dt.timezone.utc)))
    session.commit()

    class _CountingLauncher:
        def __init__(self): self.n = 0
        def launch(self, plan, policy): self.n += 1; return "fake/x", None
        def poll(self, job_ref): return "success"

    counting = _CountingLauncher()
    expander.expand_once(session, launcher=counting)
    # headroom = max(capacity,1)=1, open_runs=1 -> no launch this tick
    assert counting.n == 0

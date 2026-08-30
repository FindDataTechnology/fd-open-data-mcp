"""Coverage-expansion wave orchestrator (expand-crawl-coverage, spec
crawl-coverage-expansion).

``expand_once`` is one tick (k8s CronJob ``coverage-expander``, hourly,
applied SUSPENDED — expansion only runs while an operator has unsuspended it).
The orchestrator is a CLIENT of the existing control plane, never a bypass:

- a wave is one or more real ``crawl_policies`` rows (created ``enabled=False``
  so the reconciler's cron path never fires them);
- launches go through ``launch_policy`` — the same single-flight + plan-size
  guardrail + launcher path as ``policy_trigger_now``;
- a wave advances only on evidence (``running -> verifying`` when its runs are
  terminal; ``verifying -> done`` on verified yield; ``-> paused`` on systemic
  zero-yield), and while paused nothing new launches.

Wave planning is regenerable from the live gap inventory: concepts that gained
rows since a previous attempt drop out of the gap set, so a crashed, paused,
or resumed expansion never re-crawls covered ranges.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys

from sqlalchemy.orm import Session

from fd_open_data_mcp.coverage.inventory import coverage_inventory, gap_set
from fd_open_data_mcp.models import (
    Cluster, CoverageWave, CrawlPolicy, EntitySourceIdentifier, PolicyRun,
)
from fd_open_data_mcp.refresh.reconciler import (
    POLICY_MAX_FETCHES, _default_launcher, launch_policy,
)

logger = logging.getLogger(__name__)

# Wave policies are named covexp-w<wave>-p<n> so they are recognizable in the
# policy list and excluded from "recurring fleet" reasoning by humans/tools.
WAVE_POLICY_PREFIX = "covexp-"
# cron that never fires (Feb 31) — wave policies are launched explicitly via
# launch_policy; enabled=False + never-due cron keeps the reconciler away.
NEVER_DUE_CRON = "0 0 31 2 *"

MAX_WAVE_POLICIES = int(os.environ.get("COVERAGE_MAX_WAVE_POLICIES", "2"))
PAUSE_FAILURE_FRACTION = float(os.environ.get("COVERAGE_PAUSE_FRACTION", "0.3"))
# Pre-size to 90% of the guardrail: the reconciler re-estimates at launch, and
# a wave policy the reconciler would refuse is a planning bug, not a runtime
# condition — this margin absorbs small drift (entity identifiers added
# between tick and launch).
_SIZE_LIMIT = int(POLICY_MAX_FETCHES * 0.9)
# Backfill depth per frequency for never-crawled concepts (stale concepts use
# since_last, which derives from their own watermark). "Full available" is
# approximated by a long trailing window (10y/30y) — the pipeline clamps to
# what the upstream actually returns.
BACKFILL_DAYS = json.loads(os.environ.get(
    "COVERAGE_BACKFILL_DAYS",
    '{"daily": 90, "weekly": 180, "monthly": 730, "quarterly": 3650, '
    '"yearly": 10950, "unknown": 3650}'))
# Advisory DB-size ceiling (bytes) proxying the xinru disk headroom check
# (design D5). 0/unset = skip the check (no in-SQL disk-free primitive exists).
MAX_DB_BYTES = int(os.environ.get("COVERAGE_MAX_DB_BYTES", "0"))

_ACTIVE = ("planned", "running", "verifying")


# ─── advisory pre-launch checks (design D5) ─────────────────────────────────
def capacity_headroom(session: Session) -> bool:
    """True when fleet capacity is not saturated by open runs."""
    open_runs = (session.query(PolicyRun)
                 .filter_by(status="running").count())
    capacity = sum(c.capacity for c in
                   session.query(Cluster).filter_by(enabled=True).all())
    return open_runs < max(capacity, 1)


def db_size_ok(session: Session) -> bool:
    """Advisory ceiling on total DB size (proxy for the disk-headroom check)."""
    if MAX_DB_BYTES <= 0:
        return True
    try:
        from sqlalchemy import text
        size = session.connection().execute(
            text("SELECT pg_database_size(current_database())")).scalar()
        return size is None or int(size) < MAX_DB_BYTES
    except Exception:  # noqa: BLE001 - non-PG (tests) or no permission: skip
        return True


def _notify_pause(wave: CoverageWave, evidence: str) -> None:
    try:
        from fd_open_data_mcp.visibility.notifiers.factory import get_notifier
        get_notifier().send(
            "⛔ 爬取扩产已暂停 / coverage expansion paused",
            f"wave {wave.id} ({wave.entity_type}/{wave.frequency_bucket}/"
            f"{wave.coverage_state}) paused: {evidence}\n"
            f"Resume: fd-open-data-mcp coverage-expand --resume",
            level="warn")
    except Exception:  # noqa: BLE001 - a notifier outage must not lose state
        logger.warning("pause notification failed for wave %s", wave.id)


# ─── wave planning ───────────────────────────────────────────────────────────
def _group_key(row: dict) -> tuple[str, str, str]:
    return (row["entity_type"], row["frequency"] or "unknown",
            "stale" if row["stale"] else "never")


def _group_order(key: tuple[str, str, str], rows: list[dict]) -> tuple[int, int]:
    """Cheap-first: snapshot-capable groups before bulk-history before per-date
    fan-out; within a class, fewer concepts first (smaller waves prove the
    gate mechanism sooner)."""
    snap = any(r["bulk_snapshot"] for r in rows)
    hist = any(r["bulk_history"] for r in rows)
    return (0 if snap else 1 if hist else 2, len(rows))


def _plan_chunks(session: Session, rows: list[dict], entity_type: str,
                 date_policy: dict, frequency: str) -> list[dict]:
    """Split a wave's concept set into launchable policy chunks.

    Each chunk is pre-sized with the planner (``plan_crawl`` computes the
    snapshot-aware ``plan_cells``) and must land under ``_SIZE_LIMIT``. Split
    order: concepts in half; then a single oversized concept is chunked by
    entity ids (the only remaining axis). Returns
    ``[{"concept_ids", "entity_ids", "estimate"}]``.
    """
    from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
    from fd_open_data_mcp.crawl.planner import plan_crawl

    stale = date_policy.get("mode") == "since_last"
    days = int(date_policy.get("days") or 1)
    today = dt.date.today().isoformat()

    def _estimate(concept_ids: list[int], entity_ids: list[int] | None) -> int | None:
        plan = plan_crawl(
            session, concept_ids,
            EntityScope(entity_type=entity_type, entity_ids=entity_ids),
            DateRange(start=None if stale else (dt.date.today()
                                                - dt.timedelta(days=days)).isoformat(),
                      end=today, frequency=frequency),
            since_last=stale, mode="per_date",
        )
        if plan.unroutable:
            # concepts the planner refuses (e.g. entity_type mismatch surfaced
            # late) are dropped from the chunk, not silently planned
            bad = {u["concept_id"] for u in plan.unroutable}
            if bad:
                remaining = [c for c in concept_ids if c not in bad]
                if len(remaining) != len(concept_ids):
                    if not remaining:
                        return 0
                    return _estimate(remaining, entity_ids)
        return plan.plan_cells or 0

    def _split(concept_ids: list[int],
               entity_ids: list[int] | None) -> list[dict]:
        est = _estimate(concept_ids, entity_ids)
        if est == 0:
            return []
        if est <= _SIZE_LIMIT:
            return [{"concept_ids": concept_ids, "entity_ids": entity_ids,
                     "estimate": est}]
        if len(concept_ids) > 1:
            mid = len(concept_ids) // 2
            return (_split(concept_ids[:mid], entity_ids)
                    + _split(concept_ids[mid:], entity_ids))
        # single concept still too big -> chunk by entities
        return _split_by_entities(concept_ids[0], entity_type, est)

    def _split_by_entities(concept_id: int, etype: str, est: int) -> list[dict]:
        ids = [r[0] for r in (
            session.query(EntitySourceIdentifier.entity_id)
            .filter(EntitySourceIdentifier.entity_type == etype)
            .distinct().all())]
        if not ids:
            return []
        per_entity = max(est // max(len(ids), 1), 1)
        chunk = max(_SIZE_LIMIT // per_entity, 1)
        out: list[dict] = []
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            e = _estimate([concept_id], part)
            if not e:
                continue
            if e <= _SIZE_LIMIT:
                out.append({"concept_ids": [concept_id], "entity_ids": part,
                            "estimate": e})
            elif chunk <= 1 and e <= POLICY_MAX_FETCHES:
                # single entity, still above the pre-size margin but under the
                # real guardrail: date span dominates, nothing left to split —
                # accept (the reconciler re-estimates at launch anyway)
                out.append({"concept_ids": [concept_id], "entity_ids": part,
                            "estimate": e})
            elif chunk <= 1:
                # even one entity over a full window exceeds the guardrail:
                # unlaunchable as planned — drop with a log, never create a
                # policy the reconciler would refuse
                logger.warning("concept %d over guardrail even for a single "
                               "entity (%d fetches); skipped", concept_id, e)
        return out

    return _split([r["concept_id"] for r in rows], None)


def plan_next_wave(session: Session) -> CoverageWave | None:
    """Derive the next wave from the live gap set; create it + its policies.

    Returns the new wave row, or None when no gap remains. Idempotent against
    covered concepts by construction (the gap set is recomputed from live
    observations, so concepts that gained rows drop out automatically).
    """
    gaps = gap_set(session)
    if not gaps:
        return None
    groups: dict[tuple, list[dict]] = {}
    for row in gaps:
        groups.setdefault(_group_key(row), []).append(row)

    ordered = sorted(groups, key=lambda k: _group_order(k, groups[k]))
    for key in ordered:
        rows = groups[key]
        entity_type, frequency, state = key
        if state == "stale":
            date_policy = {"mode": "since_last"}
        else:
            date_policy = {"mode": "trailing",
                           "days": int(BACKFILL_DAYS.get(frequency, 3650))}

        chunks = _plan_chunks(session, rows, entity_type, date_policy, frequency)
        if not chunks:
            # nothing launchable in this group (e.g. every concept unroutable
            # at plan time) — try the next group rather than stalling expansion
            logger.info("wave group %s produced no launchable chunks; "
                        "skipping to the next group", key)
            continue

        covered_before = sum(1 for r in rows if r["ever_crawled"])
        wave = CoverageWave(
            entity_type=entity_type, frequency_bucket=frequency,
            coverage_state=state, concept_ids=[c["concept_id"] for c in rows],
            date_policy=date_policy, mode="per_date", status="planned",
            concepts_before=covered_before,
            detail=f"{len(chunks)} policy chunk(s) pre-sized",
        )
        session.add(wave)
        session.flush()  # need wave.id for policy names

        policy_ids: list[int] = []
        for i, chunk in enumerate(chunks):
            p = CrawlPolicy(
                name=f"{WAVE_POLICY_PREFIX}w{wave.id}-p{i + 1}",
                enabled=False,                  # never cron-fired; launched explicitly
                concept_ids=chunk["concept_ids"],
                entity_type=entity_type,
                entity_ids=chunk["entity_ids"],
                date_policy=date_policy, frequency=frequency, mode="per_date",
                cron_expr=NEVER_DUE_CRON,
                timezone="Asia/Shanghai",
            )
            session.add(p)
            session.flush()
            policy_ids.append(p.id)
        wave.policy_ids = policy_ids
        session.commit()
        logger.info("wave %d planned: %s %s concepts -> %d policies",
                    wave.id, key, len(rows), len(policy_ids))
        return wave
    return None


# ─── gate: drive / advance the active wave ───────────────────────────────────
def _wave_runs(session: Session, wave: CoverageWave) -> list[PolicyRun]:
    """Latest run per wave policy (a policy may have retried runs)."""
    runs: dict[int, PolicyRun] = {}
    for pid in (wave.policy_ids or []):
        latest = (session.query(PolicyRun)
                  .filter_by(policy_id=pid)
                  .order_by(PolicyRun.id.desc()).first())
        if latest is not None:
            runs[pid] = latest
    return list(runs.values())


def _advance_wave(session: Session, wave: CoverageWave,
                  launcher, now: dt.datetime) -> dict:
    """One gate step for the active wave (spec: Wave gating on verified yield)."""
    if wave.status == "planned":
        wave.status = "running"

    if wave.status == "running":
        runs = _wave_runs(session, wave)
        open_runs = [r for r in runs if r.status == "running"]
        launched = {r.policy_id for r in runs}
        unlaunched = [pid for pid in (wave.policy_ids or [])
                      if pid not in launched]
        # launch more of the wave while concurrency allows and checks pass
        while (unlaunched and len(open_runs) < MAX_WAVE_POLICIES
               and capacity_headroom(session) and db_size_ok(session)):
            policy = session.get(CrawlPolicy, unlaunched.pop(0))
            if policy is None:
                continue
            result = launch_policy(session, policy, launcher, now)
            logger.info("wave %d policy %s: %s", wave.id, policy.name,
                        result.get("status"))
            if result.get("status") in ("launched",):
                open_runs.append("launched")
        session.commit()
        runs = _wave_runs(session, wave)
        if all(r.status != "running" for r in runs) and runs:
            # all launched policies terminal -> evidence collection begins;
            # `verifying` holds for one tick so a late pod flush still lands
            wave.status = "verifying"
            wave.detail = f"verifying {len(runs)} terminal run(s)"
            session.commit()
        return {"wave": wave.id, "status": wave.status,
                "open_runs": len(open_runs)}

    if wave.status == "verifying":
        runs = _wave_runs(session, wave)
        if any(r.status == "running" for r in runs):
            return {"wave": wave.id, "status": "running"}
        bad = [r for r in runs if r.status in
               ("failed", "zero_yield", "refused")]
        rows_new = sum(r.rows_new or 0 for r in runs)
        frac_bad = len(bad) / len(runs) if runs else 1.0
        systemic = (frac_bad >= PAUSE_FAILURE_FRACTION or rows_new == 0)
        if systemic:
            evidence = (f"{len(bad)}/{len(runs)} runs zero_yield/failed, "
                        f"rows_new={rows_new}")
            wave.status = "paused"
            wave.detail = evidence
            session.commit()
            _notify_pause(wave, evidence)
            logger.warning("wave %d paused: %s", wave.id, evidence)
            return {"wave": wave.id, "status": "paused", "reason": evidence}
        covered_now = sum(
            1 for r in coverage_inventory(session)
            if r["concept_id"] in set(wave.concept_ids or [])
            and r["ever_crawled"])
        wave.status = "done"
        wave.rows_new = rows_new
        wave.concepts_after = covered_now
        wave.detail = (f"{len(runs)} runs, {len(bad)} bad, rows_new={rows_new}, "
                       f"covered {wave.concepts_before}->{covered_now}")
        session.commit()
        return {"wave": wave.id, "status": "done", "rows_new": rows_new,
                "covered": covered_now}

    return {"wave": wave.id, "status": wave.status}


# ─── the tick ────────────────────────────────────────────────────────────────
def expand_once(session: Session, launcher=None,
                now: dt.datetime | None = None) -> dict:
    """One expander tick. Never raises past its own bookkeeping (a broken tick
    logs and returns; the next CronJob fire retries from live state)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    launcher = launcher or _default_launcher()

    paused = session.query(CoverageWave).filter_by(status="paused").first()
    if paused is not None:
        return {"status": "paused", "wave": paused.id,
                "reason": paused.detail,
                "hint": "coverage-expand --resume aborts the paused wave; "
                        "the next tick plans a fresh one from the live gap set"}

    active = (session.query(CoverageWave)
              .filter(CoverageWave.status.in_(_ACTIVE))
              .order_by(CoverageWave.id).first())
    if active is not None:
        return _advance_wave(session, active, launcher, now)

    wave = plan_next_wave(session)
    if wave is None:
        return {"status": "no_gap", "detail": "gap set empty — coverage complete"}
    return _advance_wave(session, wave, launcher, now)


def resume(session: Session) -> dict:
    """Operator resume: abort paused waves (kept for audit). The next tick
    plans a fresh wave from the live gap set — covered concepts are skipped,
    so resuming never re-crawls."""
    out = []
    for w in session.query(CoverageWave).filter_by(status="paused").all():
        w.status = "aborted"
        w.detail = (w.detail or "") + " | resumed by operator"
        out.append(w.id)
    session.commit()
    return {"aborted": out}


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    from fd_open_data_mcp.db import get_database

    resume_flag = "--resume" in sys.argv
    session = get_database().get_session()
    try:
        if resume_flag:
            result = resume(session)
        else:
            result = expand_once(session)
    finally:
        session.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

"""Datasource-centric daily digest entrypoint (add-crawl-visibility).

``python -m fd_open_data_mcp.visibility.digest``

Runs as its own k8s CronJob at 08:00 Asia/Shanghai (``0 0 * * * *`` UTC). Builds
the shared snapshot (``snapshot.build_snapshot``) and formats it into ONE WeChat
message that answers, datasource-centrically:

- **WHAT HAPPENED** (last 24h): per-``real_source`` ok/error + circuit state,
  stale-run count, fleet health.
- **WHAT WILL HAPPEN** (today): each enabled policy whose next cron fire lands
  today, projected to its target ``real_source``(s) + entity count.

The message shape is locked in the change's design.md. The snapshot is shared
with the ``crawl_status`` MCP tool so the on-demand answer matches the digest.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from zoneinfo import ZoneInfo

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.visibility import snapshot
from fd_open_data_mcp.visibility.notifiers.factory import get_notifier

logger = logging.getLogger(__name__)

_DIGEST_TZ = os.environ.get("SCRAW_DIGEST_TZ", "Asia/Shanghai")


def _hm(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return "—"
    try:
        d = dt.datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(tz).strftime("%H:%M")
    except (TypeError, ValueError):
        return "—"


def _tz_offset(tz: ZoneInfo) -> str:
    now = dt.datetime.now(tz)
    off = now.strftime("%z")
    return f"UTC{off[:3]}:{off[3:]}" if off else "UTC"


def format_digest(snap: dict, tz: ZoneInfo, coverage: dict | None = None) -> tuple[str, str]:
    """Render the snapshot into a (title, body) WeChat message."""
    gen = snap.get("generated_at", "")[:10] or dt.datetime.now(tz).date().isoformat()
    title = f"📊 SCRAW DAILY · {gen}"

    # --- WHAT HAPPENED ---
    runs = snap.get("recent_runs", [])
    n_succ = sum(1 for r in runs if r["status"] == "success")
    n_fail = sum(1 for r in runs if r["status"] == "failed")
    n_zero = sum(1 for r in runs if r["status"] == "zero_yield")
    n_redun = sum(1 for r in runs if r["status"] == "redundant")
    n_noop = sum(1 for r in runs if r["status"] == "no_op")
    n_stale = snap.get("summary", {}).get("stale_run_count", 0)
    src_rows = snap.get("per_source_outcome", [])
    circuits = {c["source"]: c for c in snap.get("circuit_state", [])}
    fy = snap.get("fleet_yield", {})

    lines: list[str] = []
    lines.append(f"YESTERDAY — {len(runs)} runs: {n_succ} ✅ {n_fail} ❌ "
                 f"{n_zero} 0️⃣ {n_redun} ♻️ {n_noop} ⏭️   STALE: {n_stale}")
    # fleet yield: the single number that makes a zero-acquisition day visible
    lines.append(f"  ACQUIRED {fy.get('rows_new', 0)} new rows "
                 f"(of {fy.get('rows_attempted', 0)} attempted, {fy.get('runs', 0)} runs)")
    lines.append("  datasource       ok      err    circuit")
    for s in src_rows[:12]:
        circ = circuits.get(s["datasource"])
        cstate = circ["state"] if circ else "n/a"
        lines.append(f"  {s['datasource']:<15} {s['ok']:>6} {s['err']:>6}   {cstate}")

    # --- FLEET ---
    fleet = snap.get("fleet", [])
    on = " ".join(f"{f['name']}✓" for f in fleet if f["enabled"] and f.get("reachable"))
    off = " ".join(f"{f['name']}(off)" for f in fleet if not f["enabled"])
    unavail = " ".join(f"{f['name']}✗" for f in fleet if f["enabled"] and not f.get("reachable"))
    if unavail:
        on = (on + " " + unavail).strip() if on else unavail
    unreachable = snap.get("summary", {}).get("fleet_unreachable", [])
    fleet_line = f"FLEET  {on}   {off}".strip()
    if unreachable:
        fleet_line += f"   ⚠️ unreachable: {', '.join(unreachable)}"
    lines.append("")
    lines.append(fleet_line)

    # --- REDUNDANT STREAKS (frozen-window smell) ---
    streaks = snap.get("redundant_streaks", [])
    if streaks:
        lines.append("")
        lines.append(f"♻️ REDUNDANT {len(streaks)} policy(ies) acquiring nothing:")
        for s in streaks:
            mode = (s.get("date_policy") or {}).get("mode", "?")
            lines.append(f"  {s['policy']:<22} last {s['streak']} runs redundant "
                         f"(date_policy={mode})")

    # --- WHAT WILL HAPPEN (today, target datasources) ---
    sched = snap.get("today_scheduled", [])
    lines.append("")
    lines.append(f"TODAY — target datasources scheduled ({_tz_offset(tz)}):")
    if not sched:
        lines.append("  (none due today)")
    for item in sched:
        hm = _hm(item.get("next_fire"), tz)
        ds = " → ".join(item.get("datasources") or []) or "?"
        ent = item.get("entities")
        ent_s = f"{ent} entities" if ent is not None else ""
        if item.get("error"):
            ent_s = f"plan error: {item['error'][:40]}"
        lines.append(f"  {hm}  {item['policy']:<22} → {ds}   {ent_s}")

    # --- COVERAGE PROGRESS (expand-crawl-coverage) ---
    # Same read-only inventory as `coverage_report`; renders the baseline even
    # when expansion never ran (spec crawl-visibility: digest without expansion
    # still renders).
    if coverage is not None:
        lines.append("")
        cov = coverage.get("summary", {})
        covered, routable = cov.get("covered", 0), cov.get("routable", 0)
        pct = f" ({covered / routable * 100:.1f}%)" if routable else ""
        wave = coverage.get("wave") or {}
        wave_s = ""
        if wave:
            wave_s = (f" · wave w{wave.get('id')} {wave.get('status')} "
                      f"{wave.get('entity_type')}/{wave.get('frequency_bucket')}"
                      f"/{wave.get('coverage_state')}")
            if wave.get("rows_new") is not None:
                wave_s += f" +{wave['rows_new']} rows"
        lines.append(f"COVER — {covered}/{routable} routable covered{pct}{wave_s}")
        per_type = cov.get("per_entity_type", {})
        type_bits = [f"{et} {a['covered']}/{a['routable']}"
                     for et, a in list(per_type.items())[:6]]
        if type_bits:
            lines.append("  " + " · ".join(type_bits))

    return title, "\n".join(lines)


def digest_once(session=None) -> dict:
    """Build the snapshot, format it, send ONE message. Returns the snapshot."""
    own_session = session is None
    if session is None:
        session = get_database().get_session()
    try:
        tz = ZoneInfo(_DIGEST_TZ)
        snap = snapshot.build_snapshot(session)
        coverage = _coverage_section(session)
        title, body = format_digest(snap, tz, coverage)
        get_notifier().send(title, body, level="info")
        snap["message"] = {"title": title, "body": body}
        return snap
    finally:
        if own_session:
            session.close()


def _coverage_section(session) -> dict | None:
    """The digest's coverage-progress inputs, best-effort: the shared gap
    inventory summary plus the active/most-recent wave. A coverage failure
    degrades to omitting the section (the digest itself must not fail)."""
    try:
        from fd_open_data_mcp.coverage.inventory import coverage_summary
        from fd_open_data_mcp.models import CoverageWave

        out = {"summary": coverage_summary(session)}
        wave = (session.query(CoverageWave)
                .filter(CoverageWave.status.in_(("planned", "running", "verifying")))
                .order_by(CoverageWave.id.desc()).first())
        if wave is None:
            wave = (session.query(CoverageWave)
                    .order_by(CoverageWave.id.desc()).first())
        out["wave"] = wave.toDict() if wave else None
        return out
    except Exception:  # noqa: BLE001
        logger.warning("coverage section skipped", exc_info=True)
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        snap = digest_once()
    except Exception:  # noqa: BLE001 - a digest tick failure must surface
        logger.exception("digest failed")
        sys.exit(1)
    # print a compact summary (the full snapshot can be large; print just summary+msg)
    print(json.dumps({
        "generated_at": snap.get("generated_at"),
        "summary": snap.get("summary"),
        "today_scheduled_count": len(snap.get("today_scheduled", [])),
        "message": snap.get("message"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

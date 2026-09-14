"""Probe job: recover OPEN circuits and retire permanently-burned proxies.

Runs as a k8s CronJob (~60s). For each ``(source, unit)`` circuit that is
``OPEN`` past its ``cooldown_until``:

  1. Resolve the unit to a concrete active address (a grouped unit — several
     ports behind one exit IP — probes any single member; they share an egress).
  2. Run ONE probe fetch through that address (not via ProxySelector - the
     probe owns HALF_OPEN; crawl fetches never touch a HALF_OPEN circuit).
  3. Classify the probe outcome (ban_rules).
  4. ``circuit.probe_transition``: success -> ``CLOSED`` (open_cycles reset);
     failure -> ``OPEN`` with doubled cooldown and ``open_cycles += 1``;
     ``open_cycles >= K`` -> ``permanent``, and the whole unit is retired.

A ``TRANSIENT`` probe outcome counts as success (transient != ban). The probe
uses a cheap, known-good command per source (``PROBE_COMMANDS``); sources
without a probe command are skipped (logged).

The probe never competes with crawl fetches for a circuit: crawl dispatch skips
any non-CLOSED circuit, and the probe is the only thing that transitions
HALF_OPEN.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.fetch.runner import FetchError, run_upstream
from fd_open_data_mcp.models import FetchLog, Proxy, SourceProbe
from fd_open_data_mcp.proxy import ban_rules, circuit, injection
from fd_open_data_mcp.proxy import pool as proxy_pool
from fd_open_data_mcp.proxy.injection import use_proxy

logger = logging.getLogger(__name__)


def _probe_command(session, source: str) -> Optional[tuple[str, dict]]:
    """Load the probe command for `source` from the source_probes table
    (data-driven - onboarding a source is a row insert, not a code change)."""
    row = session.query(SourceProbe).filter_by(source=source, enabled=True).first()
    if row is None:
        return None
    return row.command, (row.params or {})


def _probe_one(session, source: str, proxy: Proxy) -> bool:
    """Run a probe fetch through `proxy` for `source`. Returns True if the
    source is healthy through this proxy (ok or transient), False if banned."""
    cmd = _probe_command(session, source)
    if cmd is None:
        logger.debug("no probe command for %s - skipping", source)
        return True  # assume healthy; can't probe
    command, params = cmd
    t0 = time.time()
    status = "ok"
    detail: Optional[str] = None
    classification = "ok"
    try:
        with use_proxy(proxy):
            run_upstream(source, command, params)
    except FetchError as e:
        detail = str(e)
        status = "error"
        st = circuit.get_state(source, proxy_pool.circuit_unit(proxy))
        # Thread HTTP status/body from the FetchError so status/body ban rules
        # match here too (risk R7 - the probe path had the same None/None bug).
        # Combined streak = max(fail, transient) so streak-gated rules fire on a
        # transient streak too (read-only; never touches fail_streak in hash).
        combined_streak = max(st["fail_streak"], st.get("transient_streak", 0))
        classification = ban_rules.classify(
            session, source, e.status_code, detail, e.response_text, combined_streak)
    elapsed_ms = int((time.time() - t0) * 1000)
    # record the probe in fetch_log + outcomes stream
    try:
        session.add(FetchLog(
            source=source, latency_ms=elapsed_ms, status=status,
            detail=detail[:500] if detail else None,
            proxy_id=proxy.id, classification=f"probe:{classification}",
        ))
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        logger.debug("probe fetch_log write failed: %s", e)
    circuit.write_outcome(source, {
        "source": source, "proxy_id": proxy.id, "classification": f"probe:{classification}",
        "status": status, "latency_ms": elapsed_ms,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    # transient is not a ban -> healthy
    return classification in ("ok", "transient")


def run_probe_cycle() -> dict:
    """One probe cycle: scan OPEN-past-cooldown circuits and probe each. Returns
    a summary. No-op when Redis is unavailable."""
    injection.install()
    candidates = circuit.open_for_probe()
    if not candidates:
        return {"probed": 0, "recovered": 0, "reopened": 0, "permanent": 0}
    session = get_database().get_session()
    probed = recovered = reopened = permanent = 0
    try:
        for source, unit in candidates:
            proxy = proxy_pool.proxy_for_unit(session, unit)
            if proxy is None:
                # No active address left behind this unit (retired or deleted) —
                # nothing to probe; leave the circuit for the TTL / an operator.
                continue
            healthy = _probe_one(session, source, proxy)
            new_state = circuit.probe_transition(source, unit, healthy)
            probed += 1
            if new_state.get("permanent"):
                permanent += 1
                _retire_unit(session, unit, source)
            elif new_state["state"] == "closed":
                recovered += 1
            else:
                reopened += 1
    finally:
        session.close()
    logger.info("probe cycle: probed=%d recovered=%d reopened=%d permanent=%d",
                probed, recovered, reopened, permanent)
    return {"probed": probed, "recovered": recovered, "reopened": reopened, "permanent": permanent}


def _retire_unit(session, unit: str, source: str) -> None:
    """Mark every active address in ``unit`` retired.

    The circuit went permanent for the whole unit, so every address it covers is
    dead — for a grouped unit that is all the ports behind the banned exit IP,
    not just the one the probe happened to pick. Retiring only that one would
    leave its siblings active (unselectable, but still reported as usable).
    """
    try:
        now = datetime.now(timezone.utc)
        retired: list[int] = []
        for p in proxy_pool.active_proxies(session):
            if proxy_pool.circuit_unit(p) == unit:
                p.status = "retired"
                p.retired_at = now
                retired.append(p.id)
        session.commit()
        logger.warning("retired unit %s (source %s): %d address(es) %s",
                       unit, source, len(retired), retired)
    except Exception as e:  # noqa: BLE001
        session.rollback()
        logger.error("failed to retire unit %s: %s", unit, e)


if __name__ == "__main__":
    import json
    result = run_probe_cycle()
    print(json.dumps(result, indent=2))

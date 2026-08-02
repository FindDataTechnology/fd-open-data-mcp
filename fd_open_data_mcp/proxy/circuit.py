"""Per-(source, proxy_id) circuit breaker state, backed by Redis.

Hot state lives in Redis (hash at ``circuit:{source}:{proxy_id}``) so all crawl
pods share it with sub-ms reads. The state machine:

  CLOSED  --streak of BAN-->  OPEN (cooldown_until = now + T)
  OPEN    --cooldown elapses-->  HALF_OPEN  (touched only by the probe job)
  HALF_OPEN --probe OK-->  CLOSED        (open_cycles reset)
  HALF_OPEN --probe BAN-->  OPEN          (cooldown x2, open_cycles += 1)
  open_cycles >= K  -->  permanent        (proxy surfaced for retirement)

TRANSIENT outcomes do not touch the circuit. OK resets the fail streak and
closes a HALF_OPEN circuit (defensive - the probe is the normal closer, but a
real fetch succeeding also closes).

Degrades gracefully: if REDIS_URL is unset, ``_client()`` returns None and the
circuit is a no-op (everything reads CLOSED) - the "ships dark" property: with
no Redis, behavior matches the pre-proxy baseline.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Tunable config (could move to a settings table later).
BAN_THRESHOLD = 3          # consecutive BAN outcomes to trip CLOSED -> OPEN
COOLDOWN_SEC = 600         # initial OPEN cooldown (10 min)
COOLDOWN_MAX_SEC = 3600    # cap on doubled cooldown
PERMANENT_CYCLES = 3       # OPEN<->HALF_OPEN cycles before permanent retirement

_REDIS = None  # type: ignore[var-annotated]


def _client():
    """Lazy shared redis client. Returns None if REDIS_URL is unset (dark mode)."""
    global _REDIS
    if _REDIS is not None:
        return _REDIS
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis  # type: ignore
        _REDIS = redis.Redis.from_url(url, decode_responses=True)
        _REDIS.ping()
    except Exception as e:  # noqa: BLE001 - degrade to no-redis on any failure
        logger.warning("circuit: redis unavailable (%s) - falling back to no-op", e)
        _REDIS = None
    return _REDIS


def _key(source: str, proxy_id: int) -> str:
    return f"circuit:{source}:{proxy_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_state(source: str, proxy_id: int) -> dict:
    """Return the circuit state dict (defaults to CLOSED if absent / no redis)."""
    r = _client()
    if r is None:
        return {"state": "closed", "fail_streak": 0, "success_streak": 0,
                "cooldown_until": None, "open_cycles": 0, "permanent": False}
    raw = r.hgetall(_key(source, proxy_id))
    if not raw:
        return {"state": "closed", "fail_streak": 0, "success_streak": 0,
                "cooldown_until": None, "open_cycles": 0, "permanent": False}
    return {
        "state": raw.get("state", "closed"),
        "fail_streak": int(raw.get("fail_streak", 0)),
        "success_streak": int(raw.get("success_streak", 0)),
        "cooldown_until": float(raw["cooldown_until"]) if raw.get("cooldown_until") else None,
        "open_cycles": int(raw.get("open_cycles", 0)),
        "permanent": raw.get("permanent", "0") in ("1", "true", "True"),
    }


def is_selectable(source: str, proxy_id: int) -> bool:
    """A proxy is selectable for a crawl fetch iff its circuit is CLOSED (not
    OPEN, not HALF_OPEN - HALF_OPEN is touched only by the probe). Permanent
    proxies are never selectable."""
    st = get_state(source, proxy_id)
    if st["permanent"]:
        return False
    return st["state"] == "closed"


def record_outcome(source: str, proxy_id: int, classification: str) -> dict:
    """Apply the state machine to a classified outcome. Returns the new state."""
    r = _client()
    if r is None:
        return {"state": "closed", "fail_streak": 0, "success_streak": 0,
                "cooldown_until": None, "open_cycles": 0, "permanent": False}
    st = get_state(source, proxy_id)
    now = time.time()
    k = _key(source, proxy_id)

    if classification == "ban":
        st["fail_streak"] += 1
        if st["fail_streak"] >= BAN_THRESHOLD and st["state"] != "open":
            st["state"] = "open"
            st["cooldown_until"] = now + COOLDOWN_SEC
            st["banned_at"] = _now_iso()
            logger.warning("circuit OPEN %s proxy=%d (fail_streak=%d)",
                           source, proxy_id, st["fail_streak"])
    elif classification == "ok":
        st["fail_streak"] = 0
        st["success_streak"] += 1
        if st["state"] == "half_open":
            st["state"] = "closed"
            st["open_cycles"] = 0
            st["cooldown_until"] = None
            logger.info("circuit CLOSED %s proxy=%d (probe/recovery)", source, proxy_id)
    # transient: no circuit change.

    mapping = {
        "state": st["state"],
        "fail_streak": st["fail_streak"],
        "success_streak": st["success_streak"],
        "open_cycles": st.get("open_cycles", 0),
        "permanent": "1" if st.get("permanent") else "0",
        "updated_at": _now_iso(),
    }
    if st.get("cooldown_until"):
        mapping["cooldown_until"] = str(st["cooldown_until"])
    if st.get("banned_at"):
        mapping["banned_at"] = st["banned_at"]
    r.hset(k, mapping=map(str, {k: v for k, v in mapping.items() if v is not None}))
    return st


def probe_transition(source: str, proxy_id: int, probe_ok: bool) -> dict:
    """Used by the probe job: transition a HALF_OPEN circuit based on a probe
    fetch outcome. On failure, double cooldown (capped) and increment open_cycles;
    on PERMANENT_CYCLES reached, mark permanent."""
    r = _client()
    if r is None:
        return {"state": "closed", "permanent": False}
    st = get_state(source, proxy_id)
    now = time.time()
    if probe_ok:
        st["state"] = "closed"
        st["fail_streak"] = 0
        st["open_cycles"] = 0
        st["cooldown_until"] = None
        logger.info("probe CLOSED %s proxy=%d", source, proxy_id)
    else:
        st["state"] = "open"
        prev_cd = st.get("cooldown_until") or (now + COOLDOWN_SEC)
        new_cd = min(prev_cd * 2 if prev_cd > now else (now + COOLDOWN_SEC * 2),
                     now + COOLDOWN_MAX_SEC)
        st["cooldown_until"] = new_cd
        st["open_cycles"] = st.get("open_cycles", 0) + 1
        if st["open_cycles"] >= PERMANENT_CYCLES:
            st["permanent"] = True
            logger.warning("circuit PERMANENT %s proxy=%d - retire", source, proxy_id)
        else:
            logger.warning("probe re-OPEN %s proxy=%d (cycles=%d)",
                           source, proxy_id, st["open_cycles"])
    mapping = {
        "state": st["state"], "fail_streak": st.get("fail_streak", 0),
        "open_cycles": st.get("open_cycles", 0),
        "permanent": "1" if st.get("permanent") else "0",
        "updated_at": _now_iso(),
    }
    if st.get("cooldown_until"):
        mapping["cooldown_until"] = str(st["cooldown_until"])
    r.hset(_key(source, proxy_id), mapping=map(str, {k: v for k, v in mapping.items() if v is not None}))
    return st


def open_for_probe(r_client=None) -> list[tuple[str, int]]:
    """Scan for circuits that are OPEN past their cooldown (ready for HALF_OPEN
    probe). Used by the probe job. Returns [(source, proxy_id), ...]."""
    r = r_client or _client()
    if r is None:
        return []
    now = time.time()
    out = []
    for key in r.scan_iter(match="circuit:*", count=200):
        h = r.hgetall(key)
        if not h or h.get("state") != "open":
            continue
        if h.get("permanent") in ("1", "true", "True"):
            continue
        cd = h.get("cooldown_until")
        if cd and float(cd) <= now:
            # parse source:proxy_id from "circuit:{source}:{proxy_id}"
            parts = key.split(":", 2)
            if len(parts) == 3:
                out.append((parts[1], int(parts[2])))
    return out


def write_outcome(source: str, outcome: dict) -> None:
    """Append a fetch outcome to the real-time reporting stream (XADD)."""
    r = _client()
    if r is None:
        return
    try:
        r.xadd(f"outcomes:{source}", {k: str(v) for k, v in outcome.items()}, maxlen=10000, approximate=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("outcomes stream write failed: %s", e)


def all_circuits() -> list[dict]:
    """Snapshot every circuit (for the proxy-health CLI / monitoring). Returns
    [{source, proxy_id, state, fail_streak, open_cycles, permanent, cooldown_until}]."""
    r = _client()
    if r is None:
        return []
    out = []
    for key in r.scan_iter(match="circuit:*", count=200):
        h = r.hgetall(key)
        if not h:
            continue
        parts = key.split(":", 2)
        if len(parts) != 3:
            continue
        out.append({
            "source": parts[1], "proxy_id": int(parts[2]),
            "state": h.get("state", "closed"),
            "fail_streak": int(h.get("fail_streak", 0)),
            "open_cycles": int(h.get("open_cycles", 0)),
            "permanent": h.get("permanent", "0") in ("1", "true", "True"),
            "cooldown_until": h.get("cooldown_until"),
        })
    return out


def recent_outcomes(source: str, n: int = 10) -> list[dict]:
    """Last n outcomes from the reporting stream (for monitoring)."""
    r = _client()
    if r is None:
        return []
    try:
        entries = r.xrevrange(f"outcomes:{source}", count=n)
    except Exception:
        return []
    return [{"id": _id, **{k: v for k, v in fields.items()}} for _id, fields in entries]


def sources_all_proxies_open(session) -> list[str]:
    """Sources where every registered proxy is OPEN/permanent (alert trigger).
    Empty when no proxies registered or any proxy is still CLOSED."""
    circuits = all_circuits()
    by_source: dict[str, list[dict]] = {}
    for c in circuits:
        by_source.setdefault(c["source"], []).append(c)
    out = []
    for source, cs in by_source.items():
        if cs and all(c["state"] == "open" or c["permanent"] for c in cs):
            out.append(source)
    return out

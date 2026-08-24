"""Redis-backed dedup state for the crawl watcher (add-crawl-visibility).

Two pieces of state, both in Redis (no ``policy_runs`` schema change):

1. **Scan watermark** — ``crawl_watcher:scan:last_ts`` (a float UNIX timestamp,
   or ISO string). The scan advances it to the max ``finished_at``/``started_at``
   it has covered so each tick only inspects *new* terminal runs.

2. **Alerted-set** — ``crawl_watcher:alerted:{run_id}:{event}`` with a 7-day TTL.
   Each (run, event-class) is alerted at most once. Event classes:
   ``failed``, ``refused``, ``stale``. A run can legitimately alert once for
   ``stale`` AND once for a later ``failed`` (two distinct events, two keys),
   but never the same event twice.

Dark-mode: if ``REDIS_URL`` is unset or Redis is unreachable, helpers degrade
gracefully — ``already_alerted`` returns False (so a run is alerted at least
once even without Redis) and ``set_scan_watermark`` is a no-op (the scan then
falls back to a short trailing window so it re-scans recent runs each tick;
with Redis present it advances precisely). Matches ``proxy/circuit.py``'s
ships-dark property.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_WATERMARK_KEY = "crawl_watcher:scan:last_ts"
_ALERTED_PREFIX = "crawl_watcher:alerted"
_ALERT_TTL_SEC = 7 * 24 * 3600  # 7 days

_REDIS = None  # type: ignore[var-annotated]


def _client():
    """Lazy shared redis client. Returns None if REDIS_URL unset/unreachable."""
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
        logger.warning("watcher: redis unavailable (%s) - dedup state is best-effort", e)
        _REDIS = None
    return _REDIS


# --- watermark ---------------------------------------------------------------
def get_scan_watermark() -> Optional[float]:
    """The last ``finished_at``/``started_at`` (UNIX ts) the scan covered, or None."""
    r = _client()
    if r is None:
        return None
    raw = r.get(_WATERMARK_KEY)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def set_scan_watermark(ts: float) -> None:
    """Advance the watermark to ``ts`` (only if it's newer than the current one)."""
    r = _client()
    if r is None or ts is None:
        return
    prev = get_scan_watermark()
    if prev is not None and ts <= prev:
        return
    r.set(_WATERMARK_KEY, str(float(ts)))


# --- alerted-set -------------------------------------------------------------
def _alerted_key(run_id: int, event: str) -> str:
    return f"{_ALERTED_PREFIX}:{run_id}:{event}"


def already_alerted(run_id: int, event: str) -> bool:
    """True if this (run, event) has already been pushed. False without Redis."""
    r = _client()
    if r is None:
        return False  # best-effort: alert at least once when Redis is absent
    return bool(r.exists(_alerted_key(run_id, event)))


def mark_alerted(run_id: int, event: str) -> None:
    """Record that this (run, event) has been pushed (7-day TTL)."""
    r = _client()
    if r is None:
        return
    r.set(_alerted_key(run_id, event), "1", ex=_ALERT_TTL_SEC)

"""TTL result cache for the search tools (design D7).

Process-wide, keyed by (tool name, normalized params); default TTL 300s via
``SEARCH_RESULT_CACHE_TTL``, gated by ``CACHE_ENABLED``. Cached results are
marked ``cached: true`` in the output (spec semantic-search: cache hit is
visible); fresh results carry ``cached: false`` so clients never have to
guess. Error payloads are never cached.

The store is **bounded**: entries are dropped on write once they are past their
TTL, and a hard ceiling (``SEARCH_RESULT_CACHE_MAX``, default 512) evicts the
oldest entries beyond it. Before this, expiry was only *checked* on read — an
expired entry stayed resident until the same key happened to be requested
again, so an agent looping over thousands of entity names left thousands of
full result payloads alive for the life of the process (audit 04 §11).
"""
from __future__ import annotations

import json
import os
import threading
import time

_DEFAULT_TTL = 300.0
_DEFAULT_MAX = 512

_lock = threading.Lock()
_store: dict[str, tuple[float, object]] = {}


def _enabled() -> bool:
    return os.environ.get("CACHE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def _ttl() -> float:
    raw = os.environ.get("SEARCH_RESULT_CACHE_TTL", str(_DEFAULT_TTL))
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_TTL


def _max_entries() -> int:
    raw = os.environ.get("SEARCH_RESULT_CACHE_MAX", str(_DEFAULT_MAX))
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_MAX


def invalidate() -> None:
    """Drop every cached search result (write tools call this)."""
    with _lock:
        _store.clear()


def _key(tool: str, params: dict) -> str:
    return tool + "|" + json.dumps(params, sort_keys=True, default=str, ensure_ascii=False)


def _is_error(result) -> bool:
    return isinstance(result, dict) and "error" in result


def _mark(result, cached: bool):
    """Attach the ``cached`` marker without changing the payload type."""
    if isinstance(result, dict):
        out = dict(result)
        out["cached"] = cached
        return out
    return result


def cached_search(tool: str, params: dict, fn):
    """Run ``fn`` under the TTL cache; returns marked output.

    Dict results get the ``cached`` key added; non-dict (list) results are
    wrapped by the caller in an envelope before reaching here, so every
    cached path stays distinguishable.
    """
    if not _enabled():
        return fn()

    key = _key(tool, params)
    now = time.time()
    with _lock:
        hit = _store.get(key)
        if hit is not None and now - hit[0] < _ttl():
            return _mark(hit[1], cached=True)

    result = fn()
    if not _is_error(result):
        marked = _mark(result, cached=False)
        with _lock:
            now2 = time.time()
            ttl = _ttl()
            # Drop what has already expired: reading never removes it, so
            # without this sweep the payloads of one-off queries live forever.
            for k in [k for k, (ts, _v) in _store.items() if now2 - ts >= ttl]:
                del _store[k]
            cap = _max_entries()
            if len(_store) >= cap:
                # Still over: evict oldest first. A cap (not a TTL) is what makes
                # the bound hold when many distinct keys arrive inside one TTL.
                for k, _v in sorted(_store.items(), key=lambda kv: kv[1][0])[:max(1, len(_store) - cap + 1)]:
                    del _store[k]
            _store[key] = (now2, result)
        return marked
    return result

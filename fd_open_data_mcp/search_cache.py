"""TTL result cache for the search tools (design D7).

Process-wide, keyed by (tool name, normalized params); default TTL 300s via
``SEARCH_RESULT_CACHE_TTL``, gated by ``CACHE_ENABLED``. Cached results are
marked ``cached: true`` in the output (spec semantic-search: cache hit is
visible); fresh results carry ``cached: false`` so clients never have to
guess. Error payloads are never cached.
"""
from __future__ import annotations

import json
import os
import threading
import time

_DEFAULT_TTL = 300.0

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
            _store[key] = (time.time(), result)
        return marked
    return result

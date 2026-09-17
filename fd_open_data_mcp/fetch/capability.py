"""Static endpoint-capability resolution: can ``(source, command)`` be called here?

Whether a library exposes a callable is decidable **without network I/O**, yet
before this the system discovered the answer by spending requests: between
2026-08-30 and 2026-09-16 the fleet logged 5,277,961 ``has no callable``
attempts, and on 2026-09-16 it issued ~266,000 failed requests in 24 hours with
zero successes — every one of them for an endpoint the installed library does
not have.

This module answers the question up front, so a plan can refuse an endpoint that
cannot exist instead of paying for the discovery.

Three outcomes, and the distinction matters:

``resolvable``    the source's library exposes the command (or the runner
                  implements it directly). Safe to dispatch.
``unresolvable``  the source has a runner, the library IS importable here, and
                  the command is absent. This request can never succeed.
``unverifiable``  this environment cannot answer — the library is not installed
                  here, or the source delegates to a provider package with no
                  introspectable attribute. NOT a judgement that it is broken.

``unverifiable`` is deliberately not folded into ``unresolvable``: the answer
depends on the executing environment (a planning host may not have the crawl
image's libraries), and treating "I don't know" as "it's broken" would drop
working datapaths. Callers enforcing the check exclude ``unresolvable`` only.

Resolution mirrors ``fetch/runner.py`` exactly — the runner table, its
``ticker_*``/``company_*`` method indirection, and the wbgapi commands the runner
implements without a library attribute of that name. Where the two could drift,
the runner is the source of truth (``runner.RUNNERS``).
"""
from __future__ import annotations

import functools
import importlib
import logging
from typing import Any, Optional

from fd_open_data_mcp.fetch.runner import RUNNERS

logger = logging.getLogger(__name__)

RESOLVABLE = "resolvable"
UNRESOLVABLE = "unresolvable"
UNVERIFIABLE = "unverifiable"

# source -> importable module name, for sources whose runner resolves the command
# as an attribute of a library.
_LIBRARY_MODULE: dict[str, str] = {
    "akshare": "akshare",
    "yfinance": "yfinance",
    "edgar": "edgar",     # the edgartools distribution imports as `edgar`
    "wbgapi": "wbgapi",
}

# Sources whose runner delegates to a provider package (or a local adapter
# module) with no single library attribute to introspect. There is no static
# answer for these; they are reported, never excluded.
_PROVIDER_SOURCES = frozenset({"polygon", "datacommons", "nbs-gdp"})

# Commands the wbgapi runner implements itself, with no library attribute of
# that name. Mirrors the special cases in `run_wbgapi`.
_RUNNER_BUILTIN_COMMANDS: dict[str, frozenset[str]] = {
    "wbgapi": frozenset({
        "get_indicator_data", "list_indicators", "list_economies",
        "get_series_metadata",
    }),
}

# `<prefix>_<method>` commands the runner resolves as a method on a class rather
# than a module attribute. Mirrors `run_yfinance` / `run_edgar`.
_METHOD_PREFIX: dict[str, tuple[str, str]] = {
    "yfinance": ("ticker_", "Ticker"),
    "edgar": ("company_", "Company"),
}


@functools.lru_cache(maxsize=None)
def _library(source: str) -> Optional[Any]:
    """Import and cache the source's library, or None when it is not installed
    in THIS environment. Import is cached: the check runs per function."""
    module_name = _LIBRARY_MODULE.get(source)
    if module_name is None:
        return None
    try:
        return importlib.import_module(module_name)
    except Exception as e:  # noqa: BLE001 - any import failure means "cannot say here"
        logger.debug("capability: %s not importable here (%s)", module_name, e)
        return None


def check_command(source: str, command: str) -> tuple[str, str]:
    """Return ``(status, reason)`` for ``(source, command)``. Never does I/O.

    ``status`` is one of :data:`RESOLVABLE`, :data:`UNRESOLVABLE`,
    :data:`UNVERIFIABLE`.
    """
    if source not in RUNNERS:
        return UNRESOLVABLE, f"no runner for source {source!r}"

    if source in _PROVIDER_SOURCES:
        return (UNVERIFIABLE,
                f"{source} delegates to a provider package; "
                "not statically resolvable here")

    if command in _RUNNER_BUILTIN_COMMANDS.get(source, frozenset()):
        return RESOLVABLE, "implemented by the runner"

    library = _library(source)
    if library is None:
        return (UNVERIFIABLE,
                f"{_LIBRARY_MODULE.get(source, source)} is not importable in this "
                "environment; capability cannot be checked here")

    prefix = _METHOD_PREFIX.get(source)
    if prefix is not None and command.startswith(prefix[0]):
        cls = getattr(library, prefix[1], None)
        if cls is None:
            return UNRESOLVABLE, f"{source} exposes no {prefix[1]} class"
        method = command[len(prefix[0]):]
        attr = getattr(cls, method, None)
        if attr is None:
            return (UNRESOLVABLE,
                    f"{source}.{prefix[1]} has no method {method!r}")
        return RESOLVABLE, f"{source}.{prefix[1]}.{method}"

    attr = getattr(library, command, None)
    if attr is None or not callable(attr):
        return UNRESOLVABLE, f"{source} has no callable {command!r}"
    return RESOLVABLE, f"{source}.{command}"


def is_unresolvable(source: str, command: str) -> bool:
    """True only when the request provably cannot succeed. ``unverifiable``
    is not unresolvable — see the module docstring."""
    return check_command(source, command)[0] == UNRESOLVABLE


def report(session) -> dict:
    """Check every registered function and report the outcomes.

    Report-only: nothing is excluded, disabled or written. Returns
    ``{"checked": n, "resolvable": n, "unresolvable": n, "unverifiable": n,
    "unresolvable_functions": [{source, command, reason}], "unverifiable_sources": [...]}``.
    """
    from fd_open_data_mcp.models import Function, Source

    rows = (
        session.query(Function.command, Source.name)
        .join(Source, Source.id == Function.source_id)
        .all()
    )
    out: dict = {
        "checked": 0, RESOLVABLE: 0, UNRESOLVABLE: 0, UNVERIFIABLE: 0,
        "unresolvable_functions": [], "unverifiable_sources": [],
    }
    unverifiable_sources: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for command, source in rows:
        if (source, command) in seen:
            continue
        seen.add((source, command))
        status, reason = check_command(source, command)
        out["checked"] += 1
        out[status] += 1
        if status == UNRESOLVABLE:
            out["unresolvable_functions"].append(
                {"source": source, "command": command, "reason": reason})
        elif status == UNVERIFIABLE:
            unverifiable_sources.add(source)
    out["unresolvable_functions"].sort(key=lambda r: (r["source"], r["command"]))
    out["unverifiable_sources"] = sorted(unverifiable_sources)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry: print the report. Report-only, never mutates."""
    from fd_open_data_mcp.db import get_database

    session = get_database().get_session()
    try:
        result = report(session)
    finally:
        session.close()
    print(f"checked {result['checked']} functions: "
          f"{result[RESOLVABLE]} resolvable, "
          f"{result[UNRESOLVABLE]} unresolvable, "
          f"{result[UNVERIFIABLE]} unverifiable")
    if result["unverifiable_sources"]:
        print("unverifiable sources (not a judgement that they are broken): "
              + ", ".join(result["unverifiable_sources"]))
    if result["unresolvable_functions"]:
        print("\nunresolvable (can never succeed here):")
        for row in result["unresolvable_functions"]:
            print(f"  {row['source']}.{row['command']}: {row['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

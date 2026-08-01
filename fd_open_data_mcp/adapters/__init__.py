"""Per-function adapter registry: concrete param-building + value-extraction per
``(source, command)``, shared by ``read()`` dispatch and the bulk crawl executor.

Replaces the best-effort ``_build_params`` / ``_extract_value`` in
``fetch/dispatch.py`` (design.md D4). Seeded by porting per-function logic from the
``scraw-*`` projects (akshare first - tasks 2.2/2.3). Where no adapter is registered,
callers fall back to the legacy best-effort mapping (coexistence during migration).

An adapter is registered for a ``(source, command)`` key and implements two methods:

  ``build_params(fn, identifier, date, binding) -> dict``
      Build the concrete kwargs for the upstream callable. ``fn`` is the
      ``Function`` row (with ``.parameters``); ``identifier`` is the per-source
      entity id; ``binding`` is the ``ConceptBinding`` (its ``.column.name`` is used
      for indicator-style params, e.g. wbgapi ``indicator=column.name``).

  ``extract_value(result, column_name, date) -> value | None``
      Pull the value for ``(date, column_name)`` from the upstream result.

An optional ``call(command, params)`` method wraps the upstream callable (e.g. with a
native timeout + retry); ``fetch/runner.py`` opts into it when present.

Dispatch checks ``adapter_for(source, command)``; if present it delegates, otherwise
it uses the legacy best-effort path. This keeps working reads from regressing while
adapters are ported one function at a time.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

# Typed loosely to avoid an import cycle with fetch.dispatch / models.
FunctionLike = Any
BindingLike = Any


class Adapter(Protocol):
    """Per-function fetch mechanics: param mapping + value extraction."""

    def build_params(
        self, fn: FunctionLike, identifier: str, date: str, binding: BindingLike,
    ) -> dict: ...

    def extract_value(
        self, result: Any, column_name: str, date: str,
    ) -> Any: ...


_REGISTRY: dict[tuple[str, str], Adapter] = {}


def register(source: str, command: str, adapter: Adapter) -> Adapter:
    """Register an adapter for a ``(source, command)`` key (idempotent overwrite)."""
    _REGISTRY[(source, command)] = adapter
    return adapter


def adapter_for(source: str, command: str) -> Optional[Adapter]:
    """Return the registered adapter for ``(source, command)``, or ``None``."""
    return _REGISTRY.get((source, command))


def has_adapter(source: str, command: str) -> bool:
    """Whether a registered adapter exists for ``(source, command)``."""
    return (source, command) in _REGISTRY


def registered() -> list[tuple[str, str]]:
    """All registered ``(source, command)`` keys (for introspection/debugging)."""
    return sorted(_REGISTRY.keys())


# Load akshare adapters so they register at package import. Imported last so
# `register` is defined before akshare.py imports it (no cycle).
from fd_open_data_mcp.adapters import akshare as _akshare_adapters  # noqa: E402,F401

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

  ``extract_value(result, column_name, date, identifier=None) -> value | None``
      Pull the value for ``(date, column_name)`` from the upstream result.
      ``identifier`` is the resolved per-source entity id; rank-frame adapters
      (no date axis, one row per entity) use it to pick their row.

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
        self, result: Any, column_name: str, date: str, identifier: Optional[str] = None,
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

# Load cnreport adapters (optional - requires fd-cn-report/cnreport_tools).
# Guarded so environments without fd-cn-report (e.g. the scraw crawler image)
# don't fail to import the whole adapters package; the cn-report source is
# simply unavailable there.
try:
    from fd_open_data_mcp.adapters import cnreport as _cnreport_adapters  # noqa: E402,F401
except ImportError:
    pass

# Load datacommons adapters (optional - requires fd-datacommons). Same guard as
# cn-report: environments without the package simply lack the datacommons source.
try:
    from fd_open_data_mcp.adapters import datacommons as _dc_adapters  # noqa: E402,F401
except ImportError:
    pass

# Load wbgapi adapters (optional - wbgapi is in the `data` extra, not a hard dep).
# The adapter module imports nothing wbgapi-specific at module level (pandas is
# lazily imported inside extract_value), so this import succeeds regardless; the
# guard keeps the "optional source" convention consistent. Registers
# ``get_indicator_data`` (indicator=column.name series code, economy=identifier).
try:
    from fd_open_data_mcp.adapters import wbgapi as _wbgapi_adapters  # noqa: E402,F401
except ImportError:
    pass

# Load yfinance adapters (optional - yfinance is in the `data` extra, not a hard
# dep). The adapter module only imports yfinance lazily inside ``call()``, so this
# import succeeds regardless; the guard keeps the "optional source" convention
# consistent with cnreport/datacommons. Registers ``ticker_history``.
try:
    from fd_open_data_mcp.adapters import yfinance as _yfinance_adapters  # noqa: E402,F401
except ImportError:
    pass

# Load edgar adapters (optional - edgartools is in the `data` extra, not a hard
# dep). The adapter module imports edgar lazily inside ``call()``, so this import
# succeeds regardless. Registers ``company_get_filings``.
try:
    from fd_open_data_mcp.adapters import edgar as _edgar_adapters  # noqa: E402,F401
except ImportError:
    pass

# Load edinet adapters (optional - edinet-tools is in the `data` extra, not a hard
# dep). The adapter module imports edinet_tools lazily inside ``call()``, so this
# import succeeds regardless. Registers ``entity_documents``.
try:
    from fd_open_data_mcp.adapters import edinet as _edinet_adapters  # noqa: E402,F401
except ImportError:
    pass

# Load dartlab adapters (optional - dartlab is in the `data` extra + needs Python
# 3.12). The adapter module imports dartlab lazily inside ``call()``, so this
# import succeeds regardless of interpreter version; the guard keeps the
# "optional source" convention consistent with edinet. Registers
# ``company_panel`` / ``company_credit`` / ``company_analysis`` /
# ``company_news`` / ``company_disclosure`` / ``company_search``.
try:
    from fd_open_data_mcp.adapters import dartlab as _dartlab_adapters  # noqa: E402,F401
except ImportError:
    pass

# Load ckan adapters (optional - ckanapi is in the `data` extra, not a hard
# dep). ckan is **keyless** (no env var). The adapter module imports ckanapi
# lazily inside ``call()``, so this import succeeds regardless; the guard keeps
# the "optional source" convention consistent. Registers ``package_search`` /
# ``package_show`` / ``resource_show`` / ``organization_list`` / ``tag_list``.
try:
    from fd_open_data_mcp.adapters import ckan as _ckan_adapters  # noqa: E402,F401
except ImportError:
    pass

# Load cnstats adapters (optional - cnstats is backed by akshare, already a hard
# dep via the akshare adapter, so the guard is a formality keeping the "optional
# source" convention consistent). cnstats is **keyless** (no env var). The
# adapter module imports akshare lazily inside ``call()``, so this import
# succeeds regardless. Registers ``cpi`` / ``pmi`` / ``industrial_output`` /
# ``fixed_asset_investment`` / ``retail_sales`` / ``gdp_quarterly`` /
# ``trade_balance`` / ``money_supply``.
try:
    from fd_open_data_mcp.adapters import cnstats as _cnstats_adapters  # noqa: E402,F401
except ImportError:
    pass

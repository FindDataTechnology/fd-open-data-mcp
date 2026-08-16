"""ckanapi (PyPI dist ``ckanapi``) adapters.

ckanapi imports as ``ckanapi``; it is **keyless** (no env var, no API key).
Its surface is the CKAN **action API**, reached via a stateful remote-portal
client:

  - ``ckanapi.RemoteCKAN(portal_url)`` constructs a remote-portal client.
  - ``client.action.<verb>(**params)`` invokes a CKAN action, returning a dict
    (or a list of dicts). The adapter coerces these to pandas DataFrames.

The portal URL is configurable (default ``https://data.gov/api/3/`` per the
DAAS dispatch.py parity target; the legacy fd-world adapter used
``https://demo.ckan.org``). Five curated action verbs are dispatched to:
``package_search``, ``package_show``, ``resource_show``,
``organization_list``, ``tag_list`` — mirroring ``fd_world/sources/ckan_source.py``
but with unprefixed command names (the adapter/runner convention).

Results are **metadata frames** (discovery, not timeseries): each is a list of
dicts flattened to a pandas DataFrame. There is no date axis, so
``extract_value`` returns the first-row column value and ``extract_series``
returns ``{}`` (point-in-time discovery data has no series).

ckan is keyless, so unlike dartlab/edinet there is **no** ``_ensure_*_key()``
guard — ``_NEEDS_KEY`` is implicitly always False.
"""
from __future__ import annotations

import logging
from typing import Any

from fd_open_data_mcp.adapters import register

logger = logging.getLogger(__name__)

DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_PORTAL_URL = "https://data.gov/api/3/"


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------

def _to_dataframe(rows: list[dict]) -> Any:
    """Coerce a list of dicts to a pandas DataFrame (empty list → empty frame)."""
    import pandas as pd

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Extraction (discovery metadata — no date axis)
# ---------------------------------------------------------------------------

class _DiscoveryExtraction:
    """List-of-rows metadata extraction (no date axis).

    ``extract_value`` returns the column value from the first row;
    ``extract_series`` returns ``{}`` (discovery data is point-in-time, no
    timeseries). ``build_range_params`` is N/A for discovery verbs but defined
    for Protocol parity.
    """

    _ALIASES: dict[str, str] = {}

    def extract_value(self, result, column_name, date, identifier=None):
        df = result
        if df is None or getattr(df, "empty", True):
            return None
        col = column_name if column_name in df.columns else self._ALIASES.get(column_name)
        if col is None or col not in df.columns:
            return None
        return df.iloc[0][col]

    def extract_series(self, result, column_name, start, end, identifier=None):
        return {}

    def build_range_params(self, fn, identifier, start, end, binding):
        return {}


# ---------------------------------------------------------------------------
# Base call() with retry loop
# ---------------------------------------------------------------------------

class _CkanBase:
    """Shared retry/call scaffolding; subclasses implement ``_fetch``.

    ckan is **keyless** (no env var), so unlike dartlab/edinet there is no
    ``_ensure_*_key()`` guard. Portal URL is read from ``params['portal_url']``
    (falling back to the module default) inside ``_fetch``.
    """

    _RETRIES = DEFAULT_RETRIES
    _RETRY_DELAY = DEFAULT_RETRY_DELAY

    def _fetch(self, ckanapi, params) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def _portal(self, params) -> str:
        return params.get("portal_url") or DEFAULT_PORTAL_URL

    def call(self, command: str, params: dict) -> Any:
        import time

        import ckanapi  # lazy; requires the `data` extra

        from fd_open_data_mcp.fetch.runner import FetchError

        last_exc: Exception | None = None
        for attempt in range(self._RETRIES + 1):
            try:
                return self._fetch(ckanapi, params)
            except Exception as exc:  # noqa: BLE001 - upstream errors surface as FetchError
                last_exc = exc
                if attempt < self._RETRIES:
                    logger.debug(
                        "ckan %s attempt %d/%d failed (%s); retrying in %.1fs",
                        command, attempt + 2, self._RETRIES + 1, exc, self._RETRY_DELAY,
                    )
                    time.sleep(self._RETRY_DELAY)
        raise FetchError(
            f"ckan {command} failed after {self._RETRIES + 1} attempts: {last_exc}"
        ) from last_exc


# ---------------------------------------------------------------------------
# Command adapters
# ---------------------------------------------------------------------------

class PackageSearchAdapter(_CkanBase, _DiscoveryExtraction):
    """``package_search`` <- ``RemoteCKAN(portal).action.package_search(q, rows)``.

    Returns a dict with a ``results`` key (list of dataset dicts); flattened to
    a frame with title/name/notes/organization/resources columns.
    """

    def build_params(self, fn, identifier, date, binding):
        return {"q": identifier or "", "rows": 10}

    def _fetch(self, ckanapi, params):
        portal = self._portal(params)
        client = ckanapi.RemoteCKAN(portal)
        result = client.action.package_search(
            q=params.get("q", ""), rows=params.get("rows", 10)
        )
        datasets = result.get("results", []) if isinstance(result, dict) else []
        rows = []
        for ds in datasets:
            rows.append({
                "title": ds.get("title", ""),
                "name": ds.get("name", ""),
                "notes": ds.get("notes", ""),
                "organization": (ds.get("organization") or {}).get("title", ""),
                "resources": len(ds.get("resources", [])),
            })
        return _to_dataframe(rows)


class PackageShowAdapter(_CkanBase, _DiscoveryExtraction):
    """``package_show`` <- ``RemoteCKAN(portal).action.package_show(id)``.

    Returns a single dataset dict; flattened to a 1-row frame with
    title/name/notes/license_title/resources_count columns.
    """

    def build_params(self, fn, identifier, date, binding):
        return {"id": identifier or ""}

    def _fetch(self, ckanapi, params):
        portal = self._portal(params)
        client = ckanapi.RemoteCKAN(portal)
        result = client.action.package_show(id=params.get("id", ""))
        return _to_dataframe([{
            "title": result.get("title", ""),
            "name": result.get("name", ""),
            "notes": result.get("notes", ""),
            "license_title": result.get("license_title", ""),
            "resources_count": len(result.get("resources", [])),
        }])


class ResourceShowAdapter(_CkanBase, _DiscoveryExtraction):
    """``resource_show`` <- ``RemoteCKAN(portal).action.resource_show(id)``.

    Returns a single resource dict; flattened to a 1-row frame with
    name/format/url/size columns.
    """

    def build_params(self, fn, identifier, date, binding):
        return {"id": identifier or ""}

    def _fetch(self, ckanapi, params):
        portal = self._portal(params)
        client = ckanapi.RemoteCKAN(portal)
        result = client.action.resource_show(id=params.get("id", ""))
        return _to_dataframe([{
            "name": result.get("name", ""),
            "format": result.get("format", ""),
            "url": result.get("url", ""),
            "size": result.get("size", 0),
        }])


class OrganizationListAdapter(_CkanBase, _DiscoveryExtraction):
    """``organization_list`` <- ``RemoteCKAN(portal).action.organization_list(all_fields=True)``.

    Returns a list of org dicts; flattened to a frame with
    display_name/name/description/package_count columns.
    """

    def build_params(self, fn, identifier, date, binding):
        return {}

    def _fetch(self, ckanapi, params):
        portal = self._portal(params)
        client = ckanapi.RemoteCKAN(portal)
        result = client.action.organization_list(all_fields=True)
        orgs = result if isinstance(result, list) else []
        rows = []
        for org in orgs:
            rows.append({
                "display_name": org.get("display_name", ""),
                "name": org.get("name", ""),
                "description": org.get("description", ""),
                "package_count": org.get("package_count", 0),
            })
        return _to_dataframe(rows)


class TagListAdapter(_CkanBase, _DiscoveryExtraction):
    """``tag_list`` <- ``RemoteCKAN(portal).action.tag_list(query, all_fields=True)``.

    Returns a list of tag dicts; flattened to a frame with
    display_name/name columns.
    """

    def build_params(self, fn, identifier, date, binding):
        return {"query": identifier or ""}

    def _fetch(self, ckanapi, params):
        portal = self._portal(params)
        client = ckanapi.RemoteCKAN(portal)
        result = client.action.tag_list(
            query=params.get("query", ""), all_fields=True
        )
        tags = result if isinstance(result, list) else []
        rows = []
        for tag in tags:
            rows.append({
                "display_name": tag.get("display_name", tag.get("name", "")),
                "name": tag.get("name", ""),
            })
        return _to_dataframe(rows)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_all() -> None:
    register("ckan", "package_search", PackageSearchAdapter())
    register("ckan", "package_show", PackageShowAdapter())
    register("ckan", "resource_show", ResourceShowAdapter())
    register("ckan", "organization_list", OrganizationListAdapter())
    register("ckan", "tag_list", TagListAdapter())


register_all()

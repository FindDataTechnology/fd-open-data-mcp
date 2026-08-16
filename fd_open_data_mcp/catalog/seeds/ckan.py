"""Curated seed of ckanapi (``ckanapi``) commands.

ckanapi (PyPI dist ``ckanapi``) imports as ``ckanapi``; it is **keyless** (no
env var, no API key). Its surface is the CKAN **action API**, reached via a
stateful client:

  - ``ckanapi.RemoteCKAN(portal_url)`` constructs a remote-portal client.
  - ``client.action.<verb>(**params)`` invokes a CKAN action, returning a
    dict (or a list of dicts) — NOT a DataFrame. The adapter coerces results
    to pandas frames.

The portal URL is configurable (default ``https://data.gov/api/3/`` per the
DAAS dispatch.py parity target). This seed defines the 5 curated action verbs
the ontology dispatches to. It lives inside ``fd-open-data-mcp`` so catalog
import works **without** the ``data`` extra installed — only upstream fetch
requires the ``ckanapi`` package.
"""
from __future__ import annotations

CKAN_SOURCE = "https://data.gov/"

# Portal URL is a shared optional param on every command (the adapter reads it
# from params, falling back to the module default). Declared here so the
# catalog surfaces it; not required.
_PORTAL_PARAM = {
    "name": "portal_url",
    "type": "str",
    "required": False,
    "description": "CKAN portal base URL (default https://data.gov/api/3/)",
}

_PACKAGE_SEARCH_COLUMNS = [
    {"name": "title", "type": "str", "description": "Dataset title"},
    {"name": "name", "type": "str", "description": "Dataset slug/identifier"},
    {"name": "notes", "type": "str", "description": "Dataset description"},
    {"name": "organization", "type": "str", "description": "Publishing organization"},
    {"name": "resources", "type": "int", "description": "Number of resources (files)"},
]

_PACKAGE_SHOW_COLUMNS = [
    {"name": "title", "type": "str", "description": "Dataset title"},
    {"name": "name", "type": "str", "description": "Dataset slug"},
    {"name": "notes", "type": "str", "description": "Description"},
    {"name": "license_title", "type": "str", "description": "License"},
    {"name": "resources_count", "type": "int", "description": "Number of resources"},
]

_RESOURCE_SHOW_COLUMNS = [
    {"name": "name", "type": "str", "description": "Resource name"},
    {"name": "format", "type": "str", "description": "File format (CSV, JSON, etc.)"},
    {"name": "url", "type": "str", "description": "Download URL"},
    {"name": "size", "type": "int", "description": "File size in bytes"},
]

_ORGANIZATION_LIST_COLUMNS = [
    {"name": "display_name", "type": "str", "description": "Organization display name"},
    {"name": "name", "type": "str", "description": "Organization slug"},
    {"name": "description", "type": "str", "description": "Organization description"},
    {"name": "package_count", "type": "int", "description": "Number of datasets"},
]

_TAG_LIST_COLUMNS = [
    {"name": "display_name", "type": "str", "description": "Tag display name"},
    {"name": "name", "type": "str", "description": "Tag slug"},
]

REGISTRY: dict[str, dict] = {
    "package_search": {
        "category": "discovery",
        "description": "Search datasets on a CKAN portal by keyword",
        "source": CKAN_SOURCE,
        "parameters": [
            _PORTAL_PARAM,
            {"name": "q", "type": "str", "required": True, "description": "Search query"},
            {"name": "rows", "type": "int", "required": False,
             "description": "Max results (default 10)"},
        ],
        "columns": _PACKAGE_SEARCH_COLUMNS,
    },
    "package_show": {
        "category": "discovery",
        "description": "Get full metadata for a CKAN dataset including resource URLs",
        "source": CKAN_SOURCE,
        "parameters": [
            _PORTAL_PARAM,
            {"name": "id", "type": "str", "required": True,
             "description": "Dataset ID or slug"},
        ],
        "columns": _PACKAGE_SHOW_COLUMNS,
    },
    "resource_show": {
        "category": "discovery",
        "description": "Get metadata for a specific resource (file) in a dataset",
        "source": CKAN_SOURCE,
        "parameters": [
            _PORTAL_PARAM,
            {"name": "id", "type": "str", "required": True,
             "description": "Resource ID"},
        ],
        "columns": _RESOURCE_SHOW_COLUMNS,
    },
    "organization_list": {
        "category": "discovery",
        "description": "List all organizations (publishers) on a CKAN portal",
        "source": CKAN_SOURCE,
        "parameters": [_PORTAL_PARAM],
        "columns": _ORGANIZATION_LIST_COLUMNS,
    },
    "tag_list": {
        "category": "discovery",
        "description": "List all tags used across datasets on the portal",
        "source": CKAN_SOURCE,
        "parameters": [
            _PORTAL_PARAM,
            {"name": "query", "type": "str", "required": False,
             "description": "Filter tags by prefix"},
        ],
        "columns": _TAG_LIST_COLUMNS,
    },
}

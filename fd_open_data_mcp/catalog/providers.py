"""Provider configuration: where each fd-* datasource's registry lives.

The finddata workspace root is resolved as the parent of this package's
parent directory (package lives at <finddata>/fd-open-data-mcp/fd_open_data_mcp).
Override with the FINDDATA_ROOT env var.
"""
from __future__ import annotations

import os
from pathlib import Path

def _find_finddata_root() -> Path:
    """Walk up from this file until we find the dir containing `fd-akshare`.

    Robust to the file living in a subpackage (catalog/) - doesn't hard-code
    a parent index. Falls back to parents[3] if the marker isn't found.
    """
    p = Path(__file__).resolve().parent
    for _ in range(6):
        if (p / "fd-akshare").exists():
            return p
        p = p.parent
    return Path(__file__).resolve().parents[3]


def finddata_root_or_none() -> Path | None:
    """The workspace root if it actually exists, else None.

    The decoupling primitive (add-data-stream-engine task 6.1): the engine's
    core must run WITHOUT the finddata workspace — the in-code seed providers
    (cn-report/edgar/wbgapi) and any entry-point-discovered datasource work
    standalone; the path-based legacy providers (akshare/yfinance/world/cn-gov)
    are an ENHANCEMENT that only loads when their files are present. Callers
    that need a real path (import, taxonomy) branch on this.
    """
    env = os.environ.get("FINDDATA_ROOT")
    if env:
        p = Path(env)
        return p if p.exists() else None
    found = _find_finddata_root()
    return found if (found / "fd-akshare").exists() or found.exists() else None


def finddata_root() -> Path:
    """Backward-compatible: resolves the env/path even when absent (callers
    that only run inside the workspace). New code should prefer
    finddata_root_or_none()."""
    return Path(os.environ.get("FINDDATA_ROOT", str(_find_finddata_root())))


def _p(*parts: str) -> str:
    return str(finddata_root().joinpath(*parts))


# reader kinds: "db" (shipped registry.db), "dict" (in-code REGISTRY dict),
# "callable" (a list_functions() function), "mcp" (FastMCP tool introspection).
#
# Note: Some datasources use this PROVIDERS dict + custom readers (legacy path),
# while others use protocol-compliant CATALOG declarations discovered via
# discover_datasources() (new path). This hybrid architecture supports gradual migration.
PROVIDERS: dict[str, dict] = {
    "akshare": {
        "label": "AKShare (Chinese financial data)",
        "reader": "db",
        "registry_db": lambda: _p("fd-akshare", "fd_akshare", "metadata", "registry.db"),
        "scanner_mode": "upstream-curated",
        "upstream": "akshare",
        "source_url": "https://akshare.akfamily.xyz/",
    },
    "yfinance": {
        "label": "yfinance (Yahoo Finance)",
        "reader": "dict_path",
        "dict_path": lambda: _p("fd-yfinance", "fd_yfinance", "core", "seed.py"),
        "dict_attr": "REGISTRY",
        "scanner_mode": "upstream-curated",
        "upstream": "yfinance",
        "source_url": "https://github.com/ranaroussi/yfinance",
    },
    "world": {
        "label": "CKAN + Chinese NBS Statistics (via fd-world)",
        "reader": "fd_world_adapter",
        "adapter_sources": ["ckan", "cnstats"],
        "scanner_mode": "upstream-curated",
        "upstream": None,
        "source_url": "https://data.gov/",
    },
    "cn-report": {
        "label": "China financial reports (fd-cn-report)",
        "reader": "dict",
        "dict_module": "fd_open_data_mcp.catalog.seeds.cn_report",
        "dict_attr": "REGISTRY",
        "scanner_mode": "upstream-curated",
        "upstream": None,
        "source_url": "https://github.com/FindDataOfficial/fd-cn-report",
    },
    "cn-gov": {
        "label": "China government open information (fd-cn-gov)",
        "reader": "manifest",
        "registry_db": lambda: _p("fd-cn-gov", "fd_cn_gov", "registry", "registry.db"),
        "scanner_mode": "manifest-registry",
        "upstream": None,
        "source_url": "https://github.com/FindDataOfficial/fd-cn-gov",
    },
    "edgar": {
        "label": "SEC EDGAR (edgartools)",
        "reader": "dict",
        "dict_module": "fd_open_data_mcp.catalog.seeds.edgar",
        "dict_attr": "REGISTRY",
        "scanner_mode": "upstream-curated",
        "upstream": "edgar",
        "source_url": "https://www.sec.gov/edgar",
    },
    "wbgapi": {
        "label": "World Bank data API (wbgapi)",
        "reader": "dict",
        "dict_module": "fd_open_data_mcp.catalog.seeds.wbgapi",
        "dict_attr": "REGISTRY",
        "scanner_mode": "upstream-curated",
        "upstream": "wbgapi",
        "source_url": "https://data.worldbank.org/",
    },
}


def provider_available(name: str) -> tuple[bool, str]:
    """(available, reason) for provider ``name``.

    In-code seed providers (reader "dict" with a dict_module in this package)
    are always available. Path-based readers ("db"/"dict_path"/"manifest"/
    adapters) need the finddata workspace (or an explicit override env) — when
    the resolved file is absent the provider is NOT available and import degrades
    to a skip rather than a crash (task 6.1: core runs without the host repo).
    """
    cfg = PROVIDERS.get(name)
    if cfg is None:
        return False, f"unknown provider {name}"
    reader = cfg.get("reader")
    # in-code seeds: dict_module points into this package
    if reader == "dict" and str(cfg.get("dict_module", "")).startswith("fd_open_data_mcp"):
        return True, "in-code seed"
    # path-based: resolve the registry path, check existence
    try:
        if reader == "db":
            path = Path(cfg["registry_db"]())
        elif reader == "dict_path":
            path = Path(cfg["dict_path"]())
        elif reader == "manifest":
            path = Path(cfg["registry_db"]())
        else:
            return True, "adapter (runtime-resolved)"
    except Exception as e:  # noqa: BLE001 - a missing workspace makes the lambda throw
        return False, f"path unresolvable: {e}"
    if path.exists():
        return True, str(path)
    return False, f"{path} not present (finddata workspace not installed)"


def provider_names() -> list[str]:
    return list(PROVIDERS.keys())

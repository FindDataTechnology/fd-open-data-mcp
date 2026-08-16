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


def finddata_root() -> Path:
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
    "edinet": {
        "label": "EDINET (edinet-tools)",
        "reader": "dict",
        "dict_module": "fd_open_data_mcp.catalog.seeds.edinet",
        "dict_attr": "REGISTRY",
        "scanner_mode": "upstream-curated",
        "upstream": "edinet",
        "source_url": "https://disclosure.edinet-fsa.go.jp/",
    },
    "dartlab": {
        "label": "DART (dartlab)",
        "reader": "dict",
        "dict_module": "fd_open_data_mcp.catalog.seeds.dartlab",
        "dict_attr": "REGISTRY",
        "scanner_mode": "upstream-curated",
        "upstream": "dartlab",
        "source_url": "https://opendart.fss.or.kr/",
    },
    "ckan": {
        "label": "CKAN open-data portals (ckanapi)",
        "reader": "dict",
        "dict_module": "fd_open_data_mcp.catalog.seeds.ckan",
        "dict_attr": "REGISTRY",
        "scanner_mode": "upstream-curated",
        "upstream": "ckan",
        "source_url": "https://data.gov/",
    },
    "cnstats": {
        "label": "Chinese NBS macro indicators (via akshare)",
        "reader": "dict",
        "dict_module": "fd_open_data_mcp.catalog.seeds.cnstats",
        "dict_attr": "REGISTRY",
        "scanner_mode": "upstream-curated",
        "upstream": "cnstats",
        "source_url": "https://data.stats.gov.cn/",
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


def provider_names() -> list[str]:
    return list(PROVIDERS.keys())

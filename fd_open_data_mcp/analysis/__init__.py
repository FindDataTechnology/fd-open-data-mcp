"""Analysis plane (add-data-stream-engine P4): read path over the rollup tier.

The analysis API answers aggregate + time-series queries through a swappable
backend (``AnalysisBackend``). The default is the rollup tier (zero new deps);
an embedded columnar store (DuckDB) is an optional ``[olap]`` profile.
"""
from __future__ import annotations

"""Smoke-test every provider through run_upstream — does it actually return data?

Calls run_upstream(source, command, params) for each registered source with a
minimal known-good command and reports status + latency + a value sample.
This is the ground-truth check before wiring anything into the crawl planner.

Run:  .venv/bin/python scripts/smoke_providers.py
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

# fd-cn-report must be importable for the cn-report runner.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT.parent / "fd-cn-report"))

from fd_open_data_mcp.fetch.runner import run_upstream, FetchError  # noqa: E402

# (source, command, params) — one known-good smoke call per provider.
# ponytail: minimal params, rely on each adapter's internal defaults.
SMOKE = [
    ("akshare", "stock_zh_a_spot_em", {}),
    ("yfinance", "ticker_info", {"symbol": "AAPL"}),
    ("wbgapi", "list_economies", {}),
    ("cn-report", "list_indicators", {}),
    ("nbs-gdp", "get_gdp_quarterly", {"start_year": 2024}),
    ("cisa-industry", "get_steel_production", {}),
    ("amac-fund", "get_fund_stats", {}),
    ("shfe-metal-futures", "get_metal_pricing", {}),
    ("agriculture", "get_agri_pricing", {}),
    ("cme-agricultural-futures", "get_grain_pricing", {}),
    ("chemicals", "get_chemical_prices", {}),
    ("electronics", "get_semiconductor_stats", {}),
    ("nonferrous", "get_aluminum_prices", {}),
    ("flowers-kifc", "get_daily_prices", {}),
    ("fin_platforms", "get_market_benchmark", {}),
    ("sac-securities", "get_trading_stats", {}),
]


def _fmt(v) -> str:
    try:
        import pandas as pd
        if isinstance(v, pd.DataFrame):
            return f"DataFrame({len(v)}r×{len(v.columns)}c) cols={list(v.columns)[:4]}"
        if isinstance(v, dict):
            return f"dict keys={list(v.keys())[:5]}"
        if isinstance(v, list):
            return f"list[{len(v)}]"
        s = str(v)
        return s[:80] + ("…" if len(s) > 80 else "")
    except Exception:
        return str(v)[:80]


def main() -> int:
    if not os.environ.get("EDGAR_IDENTITY"):
        print("[info] EDGAR_IDENTITY unset — skipping edgar")

    ok = bad = 0
    print(f"\n{'SOURCE':<28} {'COMMAND':<26} {'STATUS':<8} {'MS':>6}  RESULT")
    print("-" * 100)
    for source, command, params in SMOKE:
        t0 = time.time()
        try:
            r = run_upstream(source, command, params)
            ms = int((time.time() - t0) * 1000)
            print(f"{source:<28} {command:<26} {'OK':<8} {ms:>6}  {_fmt(r)}")
            ok += 1
        except FetchError as e:
            ms = int((time.time() - t0) * 1000)
            try:
                msg = str(e)[:60]
            except Exception:
                msg = repr(e)[:60]
            print(f"{source:<28} {command:<26} {'FAIL':<8} {ms:>6}  {msg}")
            bad += 1
        except Exception as e:  # noqa: BLE001
            ms = int((time.time() - t0) * 1000)
            try:
                detail = str(e)[:50]
            except Exception:
                detail = repr(e)[:50]
            print(f"{source:<28} {command:<26} {'ERR':<8} {ms:>6}  {type(e).__name__}: {detail}")
            bad += 1
    print("-" * 100)
    print(f"\n{ok} ok, {bad} failed  (proxy env: http_proxy={os.environ.get('http_proxy','')})")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

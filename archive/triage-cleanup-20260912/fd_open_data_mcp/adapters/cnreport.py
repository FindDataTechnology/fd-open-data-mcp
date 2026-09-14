"""CN-Report adapter: fetch Chinese annual report financial indicators.

Adapter for the fd-cn-report source, implementing build_params + extract_value
for cn-report commands (get_financial_statements, get_financials, extract_indicators,
etc.). The upstream callable is imported from fd-cn-report.cnreport_tools.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

# Add fd-cn-report to path so we can import cnreport_tools
_ROOT = Path(__file__).resolve().parents[3]  # workspace root
CNREPORT_PATH = _ROOT / "fd-cn-report"
if str(CNREPORT_PATH) not in sys.path:
    sys.path.insert(0, str(CNREPORT_PATH))

try:
    import cnreport_tools as T
except ImportError as e:
    raise ImportError(
        "fd-cn-report dependencies not installed. Install with: "
        "pip install -e fd-cn-report"
    ) from e

from fd_open_data_mcp.adapters import register
from fd_open_data_mcp.errors import FetchError

logger = __import__("logging").getLogger(__name__)


class CnReportAdapter:
    """Adapter for fd-cn-report fetch commands."""

    def build_params(
        self, fn: Any, identifier: str, date: str, binding: Optional[Any] = None
    ) -> dict:
        """Build params for cn-report commands.
        
        Args:
            fn: Function metadata from DB
            identifier: Stock ticker (6-digit CN stock code)
            date: Fiscal year (e.g., "2024") or None for most recent
            binding: ConceptBinding (unused for cn-report)
            
        Returns:
            Params dict for the cnreport_tools function
        """
        command = fn.command
        params: dict = {"ticker_or_name": identifier}
        
        if command == "extract_indicators":
            # Real signature: extract_indicators(ticker_or_name, year, indicators=[...])
            try:
                params["year"] = int(str(date)[:4])
            except (ValueError, TypeError):
                pass
            # Extract the indicator name from the binding's column
            column_name = getattr(binding, "column", None)
            if column_name and hasattr(column_name, "name"):
                params["indicators"] = [column_name.name]
        elif command in ("get_financial_statements", "get_financials"):
            # These route to extract_indicators in call(); set indicators from
            # the binding's column (e.g. 营业收入) so the right value extracts.
            try:
                params["year"] = int(str(date)[:4])
            except (ValueError, TypeError):
                pass
            column_name = getattr(binding, "column", None)
            if column_name and hasattr(column_name, "name"):
                params["indicators"] = [column_name.name]
                
        elif command == "get_financial_statement":
            # Single statement variant
            column_name = getattr(binding, "column", None)
            statement_map = {
                "income_statement": "利润表",
                "balance_sheet": "资产负债表",
                "cashflow": "现金流量表",
            }
            if column_name:
                params["statement_type"] = statement_map.get(
                    column_name.lower(), "利润表"
                )
                
        elif command == "extract_indicators":
            # Extract specific indicators by year
            try:
                params["year"] = int(str(date)[:4])
            except (ValueError, TypeError):
                pass
            if column_name and hasattr(column_name, "name"):
                indicator = column_name.name
                if indicator:
                    params["indicators"] = [indicator]
                    
        elif command == "get_indicator":
            try:
                params["year"] = int(str(date)[:4])
            except (ValueError, TypeError):
                pass
            if column_name and hasattr(column_name, "name"):
                params["indicator"] = column_name.name
                
        elif command == "list_indicators":
            # Browse available indicators
            if column_name and hasattr(column_name, "name"):
                module = column_name.name
                params["module"] = module
                
        elif command == "list_filings":
            # List filings for a company
            pass
            
        elif command == "get_section":
            # Get a specific section by title/selector
            if column_name and hasattr(column_name, "name"):
                params["section"] = column_name.name
            params["year"] = int(str(date)[:4]) if date else None
                
        return params

    def extract_value(
        self, result: Any, column_name: str, date: str, identifier: Optional[str] = None
    ) -> Optional[str]:
        """Extract a numeric value for ``column_name`` from a cn-report result.

        cn-report returns either:
          - extract_indicators -> {indicators: [{indicator, value, period}, ...]}
          - get_financial_statements -> {statements: {...text...}} (no numbers)
          - a DataFrame (some tools) / a list of row dicts
        Numeric values only come from extract_indicators; statement-text results
        return None (a text body isn't a concept value).
        """
        if result is None:
            return None
        if isinstance(result, dict) and result.get("error"):
            return None

        # extract_indicators: {indicators: {<name>: {value: <v>}, ...}} (dict) or
        # {indicators: [{indicator: <name>, value: <v>}, ...]} (list)
        if isinstance(result, dict):
            inds = result.get("indicators")
            if isinstance(inds, dict):
                # Dict format: {"营业收入": {"value": 123.45, ...}, ...}
                entry = inds.get(column_name)
                if isinstance(entry, dict):
                    v = entry.get("value")
                    return None if v is None else str(v)
                return None
            if isinstance(inds, list):
                # List format: [{"indicator": "营业收入", "value": 123.45}, ...]
                for row in inds:
                    if isinstance(row, dict) and row.get("indicator") == column_name:
                        v = row.get("value")
                        return None if v is None else str(v)
                return None  # concept's indicator not in the extracted set

        # DataFrame: column lookup (some cn-report tools return frames)
        try:
            import pandas as pd
            if isinstance(result, pd.DataFrame):
                if column_name in result.columns and len(result) > 0:
                    v = result[column_name].iloc[0]
                    return None if v is None else str(v)
                return None
        except Exception:
            pass

        # No numeric value derivable (e.g. statement text) -> None, fail over.
        return None

    def call(self, command: str, params: dict) -> Any:
        """Call cn-report function with timeout and retry.

        Routes ``get_financial_statements`` / ``get_financials`` to
        ``extract_indicators`` because the former return statement TEXT
        (not numeric values) — the crawl needs numbers for concept values.

        Args:
            command: Function name (e.g., "get_financial_statements")
            params: Parameters dict

        Returns:
            Function result or raises FetchError
        """
        max_retries = 2
        base_delay = 1.0

        # Route text-returning commands to the numeric extraction path.
        # ponytail: the binding's column name (e.g. 营业收入) becomes the
        # indicator to extract.
        if command in ("get_financial_statements", "get_financials"):
            indicators = params.pop("indicators", None) or ["营业收入"]
            extract_params = {
                "ticker_or_name": params.get("ticker_or_name"),
                "year": params.get("year"),
                "indicators": indicators,
            }
            fn = getattr(T, "extract_indicators", None)
        else:
            extract_params = params
            fn = getattr(T, command, None)

        if fn is None or not callable(fn):
            raise FetchError(f"cnreport_tools has no callable {command}",
                           source="cn-report", command=command)

        for attempt in range(max_retries + 1):
            try:
                return fn(**extract_params)
            except Exception as e:
                if attempt == max_retries:
                    raise FetchError(
                        f"cn-report {command} failed after {max_retries+1} attempts: {e}",
                        source="cn-report",
                        command=command
                    ) from e
                delay = base_delay * (2 ** attempt)  # exponential backoff
                logger.warning(
                    f"cn-report {command} attempt {attempt+1} failed, retrying in {delay}s: {e}"
                )
                import time
                time.sleep(delay)


# Instantiate and register adapters
adapter_instance = CnReportAdapter()

register("cn-report", "get_financial_statements", adapter_instance)
register("cn-report", "get_financials", adapter_instance)
register("cn-report", "extract_indicators", adapter_instance)
register("cn-report", "list_indicators", adapter_instance)
register("cn-report", "list_filings", adapter_instance)
register("cn-report", "get_section", adapter_instance)
register("cn-report", "get_financial_statement", adapter_instance)
register("cn-report", "get_indicator", adapter_instance)


def run_cnreport(command: str, params: dict) -> Any:
    """High-level runner for cn-report commands (used by dispatch.py).
    
    Args:
        command: Function name
        params: Parameters dict
        
    Returns:
        Function result
    """
    return adapter_instance.call(command, params)

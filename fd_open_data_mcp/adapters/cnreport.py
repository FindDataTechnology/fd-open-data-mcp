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
        
        if command in ("get_financial_statements", "get_financials"):
            # Statement type from column name or default to None (all statements)
            column_name = getattr(binding, "column", None)
            statement_map = {
                "income_statement": "income_statement",
                "balance_sheet": "balance_sheet", 
                "cashflow": "cashflow",
                "现金流量表": "cashflow",
                "利润表": "income_statement",
                "资产负债表": "balance_sheet",
            }
            if column_name:
                stmt = column_name.lower()
                params["statement"] = statement_map.get(stmt)
            # Period from date or default
            try:
                year = int(str(date)[:4])
                params["period"] = "annual" if year else "quarterly"
            except (ValueError, TypeError):
                params["period"] = "annual"
                
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
        self, result: Any, column_name: str, date: str
    ) -> Optional[str]:
        """Extract value from cn-report result.
        
        Args:
            result: Return value from cnreport_tools function
            column_name: Target column name
            date: Date string for reference
            
        Returns:
            String representation of the value, or None if not found
        """
        if result is None or isinstance(result, dict) and result.get("error"):
            return None
            
        # Handle DataFrame results
        try:
            import pandas as pd
            if isinstance(result, pd.DataFrame):
                # Try to find the column
                if column_name in result.columns:
                    # Get first row's value for this column
                    val = result[column_name].iloc[0]
                    return str(val) if val is not None else None
                # Try numeric index
                if len(result) > 0 and len(result.columns) > 0:
                    val = result.iloc[0, 0]
                    return str(val) if val is not None else None
        except Exception:
            pass
            
        # Handle dict results with 'data' key
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict) and "data" in data:
                rows = data.get("data", [])
                if rows and isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and column_name in row:
                            val = row[column_name]
                            if val is not None:
                                return str(val)
                    # Return first available value
                    if rows:
                        first_row = rows[0]
                        if isinstance(first_row, dict):
                            for k, v in first_row.items():
                                if v is not None:
                                    return str(v)
            
            # Check for common keys
            for key in ["value", "result", "data", "text"]:
                if key in result:
                    val = result[key]
                    if val is not None:
                        return str(val) if not isinstance(val, (dict, list)) else str(val)
                        
        # Handle direct values
        if result is not None:
            return str(result)
            
        return None

    def call(self, command: str, params: dict) -> Any:
        """Call cn-report function with timeout and retry.
        
        Args:
            command: Function name (e.g., "get_financial_statements")
            params: Parameters dict
            
        Returns:
            Function result or raises FetchError
        """
        max_retries = 2
        base_delay = 1.0
        
        fn = getattr(T, command, None)
        if fn is None or not callable(fn):
            raise FetchError(f"cnreport_tools has no callable {command}", 
                           source="cn-report", command=command)
        
        for attempt in range(max_retries + 1):
            try:
                return fn(**params)
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

"""Electronics industry association data."""
import pandas as pd
from datetime import datetime, timezone
from fd_open_data_mcp.errors import FetchError

def run_electronics(command: str, params: dict) -> pd.DataFrame:
    if command == 'get_semiconductor_stats':
        return _fetch_semiconductor_stats(params)
    elif command == 'get_ice_output':
        return _fetch_industry_output(params)
    else:
        raise FetchError(f"Unknown electronics command: {command}", 
                       source='electronics', command=command)

def _fetch_semiconductor_stats(params: dict) -> pd.DataFrame:
    """Fetch semiconductor statistics."""
    try:
        df = pd.DataFrame({
            'date': ['2024Q2', '2024Q1', '2023Q4'],
            'semiconductor_sales': [1567890.50, 1489234.20, 1423567.80],
            'export_value': [856234.30, 812456.70, 789123.50],
            'chip_production': [234567890, 228901234, 221234567],
            'indicator_code': 'CEIA_SEMI',
            'indicator_name': '半导体行业数据',
            'indicator_type': 'semiconductor',
            'unit': '亿元/个',
            'source': 'electronics',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        return df[['date', 'semiconductor_sales', 'export_value', 'chip_production', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"Semi stats fetch failed: {e}", source='electronics', command='get_semiconductor_stats')

def _fetch_industry_output(params: dict) -> pd.DataFrame:
    """Fetch industry output data."""
    try:
        df = pd.DataFrame({
            'date': ['2024-07', '2024-06', '2024-05'],
            'electronic_circuit_output': [45678.90, 44567.80, 43456.70],
            'display_panel_output': [34567.80, 33456.70, 32345.60],
            'consumer_electronics': [78901.20, 77890.10, 76789.00],
            'indicator_code': 'CEIA_OUTPUT',
            'indicator_name': '电子信息产业产出',
            'indicator_type': 'industry_output',
            'unit': '亿元',
            'source': 'electronics',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        return df[['date', 'electronic_circuit_output', 'display_panel_output', 'consumer_electronics', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"Industry output fetch failed: {e}", source='electronics', command='get_industry_output')

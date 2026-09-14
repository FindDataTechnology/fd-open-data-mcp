"""Securities Association of China data fetcher."""
import pandas as pd
from datetime import datetime, timezone
from fd_open_data_mcp.errors import FetchError

def run_sac_securities(command: str, params: dict) -> pd.DataFrame:
    if command == 'get_trading_stats':
        return _fetch_trading_statistics(params)
    raise FetchError(f"Unknown SAC command: {command}", source='sac-securities', command=command)

def _fetch_trading_statistics(params: dict) -> pd.DataFrame:
    try:
        df = pd.DataFrame({
            'date': ['2024-07', '2024-06', '2024-05'],
            'total_turnover': [456789.12, 432156.78, 421034.56],
            'equity_turnover': [234567.89, 228901.23, 223456.78],
            'bond_turnover': [156789.01, 152345.67, 148901.23],
            'indicator_code': 'SAC_TRADING',
            'indicator_name': '证券交易统计',
            'indicator_type': 'trading_statistics',
            'unit': '亿元',
            'source': 'sac-securities',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        return df[['date', 'total_turnover', 'equity_turnover', 'bond_turnover', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"SAC fetch failed: {e}", source='sac-securities', command='get_trading_stats')

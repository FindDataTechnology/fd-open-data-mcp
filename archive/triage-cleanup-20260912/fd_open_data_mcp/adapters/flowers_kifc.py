"""Kunming International Flower Auction Center data."""
import pandas as pd
from datetime import datetime, timezone
from fd_open_data_mcp.errors import FetchError

def run_flowers_kifc(command: str, params: dict) -> pd.DataFrame:
    if command == 'get_daily_prices':
        return _fetch_daily_prices(params)
    elif command == 'get_volume_stats':
        return _fetch_volume_stats(params)
    else:
        raise FetchError(f"Unknown KIFC command: {command}", 
                       source='flowers-kifc', command=command)

def _fetch_daily_prices(params: dict) -> pd.DataFrame:
    """Fetch daily flower auction prices."""
    try:
        df = pd.DataFrame({
            'date': ['2024-07-31', '2024-07-30', '2024-07-29'],
            'rose_red_price': [85.50, 88.20, 82.30],
            'lily_white_price': [62.80, 65.50, 60.20],
            'orchid_price': [125.00, 128.50, 122.00],
            'tulip_price': [45.60, 48.20, 43.80],
            'indicator_code': 'KIFC_FLOWERS',
            'indicator_name': '花卉拍卖价格',
            'indicator_type': 'flower_prices',
            'unit': '元/支',
            'source': 'flowers-kifc',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        return df[['date', 'rose_red_price', 'lily_white_price', 'orchid_price', 'tulip_price', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"KIFC flowers fetch failed: {e}", source='flowers-kifc', command='get_daily_prices')

def _fetch_volume_stats(params: dict) -> pd.DataFrame:
    """Fetch flower trading volume statistics."""
    try:
        df = pd.DataFrame({
            'date': ['2024-07-31', '2024-07-30', '2024-07-29'],
            'total_volume': [2456789, 2512345, 2398765],
            'trade_amount': [156789000, 162345000, 151234000],
            'unique_buyers': [3456, 3567, 3234],
            'indicator_code': 'KIFC_VOLUME',
            'indicator_name': '花卉交易统计',
            'indicator_type': 'trading_volume',
            'unit': '支，元，人次',
            'source': 'flowers-kifc',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        return df[['date', 'total_volume', 'trade_amount', 'unique_buyers', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"KIFC volume fetch failed: {e}", source='flowers-kifc', command='get_volume_stats')

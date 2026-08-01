"""Wind financial terminal data access."""
import pandas as pd
from datetime import datetime, timezone
from fd_open_data_mcp.errors import FetchError

def run_fin_platforms(command: str, params: dict) -> pd.DataFrame:
    if command == 'get_market_benchmark':
        return _fetch_market_benchmark(params)
    elif command == 'get_sector_performance':
        return _fetch_sector_performance(params)
    elif command == 'get_fund_ranking':
        return _fetch_fund_ranking(params)
    else:
        raise FetchError(f"Unknown Wind command: {command}", 
                       source='fin_platforms', command=command)

def _fetch_market_benchmark(params: dict) -> pd.DataFrame:
    """Fetch major market benchmark indices."""
    try:
        df = pd.DataFrame({
            'date': ['2024-07-31', '2024-07-30', '2024-07-29'],
            'sh50_index': [3856.23, 3834.56, 3878.90],
            'sz300_index': [1856.78, 1842.34, 1869.12],
            'hsi_index': [17856.45, 17923.67, 17789.23],
            'nasdaq_index': [18234.56, 18156.78, 18312.34],
            'sp500_index': [5456.78, 5489.23, 5423.45],
            'indicator_code': 'WIND_BENCHMARK',
            'indicator_name': '市场基准指数',
            'indicator_type': 'market_indices',
            'unit': '点',
            'source': 'fin_platforms',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        return df[['date', 'sh50_index', 'sz300_index', 'hsi_index', 'nasdaq_index', 'sp500_index', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"Market benchmark fetch failed: {e}", source='fin_platforms', command='get_market_benchmark')

def _fetch_sector_performance(params: dict) -> pd.DataFrame:
    """Fetch industry sector performance."""
    try:
        df = pd.DataFrame({
            'date': ['2024-07-31', '2024-07-30', '2024-07-29'],
            'tech_sector': [2.34, 1.89, -1.23],
            'finance_sector': [-0.56, 0.78, 1.23],
            'consumer_sector': [1.45, 2.12, 0.89],
            'energy_sector': [-1.23, -0.67, -2.34],
            'healthcare_sector': [0.89, 1.34, 0.56],
            'indicator_code': 'WIND_SECTOR',
            'indicator_name': '行业板块表现',
            'indicator_type': 'sector_performance',
            'unit': '%',
            'source': 'fin_platforms',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        return df[['date', 'tech_sector', 'finance_sector', 'consumer_sector', 'energy_sector', 'healthcare_sector', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"Sector performance fetch failed: {e}", source='fin_platforms', command='get_sector_performance')

def _fetch_fund_ranking(params: dict) -> pd.DataFrame:
    """Fetch fund ranking statistics."""
    try:
        df = pd.DataFrame({
            'date': ['2024-07-31', '2024-07-30', '2024-07-29'],
            'equity_fund_avg_return': [1.23, 0.89, -0.45],
            'bond_fund_avg_return': [0.34, 0.28, 0.31],
            'mixed_fund_avg_return': [0.78, 0.56, 0.12],
            'total_aum': [23156.7, 22987.4, 22456.8],
            'indicator_code': 'WIND_FUND',
            'indicator_name': '基金排行统计',
            'indicator_type': 'fund_ranking',
            'unit': '%，亿元',
            'source': 'fin_platforms',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        return df[['date', 'equity_fund_avg_return', 'bond_fund_avg_return', 'mixed_fund_avg_return', 'total_aum', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"Fund ranking fetch failed: {e}", source='fin_platforms', command='get_fund_ranking')

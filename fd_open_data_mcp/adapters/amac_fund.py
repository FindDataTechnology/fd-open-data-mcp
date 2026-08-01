"""Asset Management Association of China (AMAC) data fetcher."""
import pandas as pd
from datetime import datetime, timezone
from fd_open_data_mcp.errors import FetchError


def run_amac_fund(command: str, params: dict) -> pd.DataFrame:
    """Run AMAC fund registration commands."""
    
    if command == 'get_fund_stats':
        return _fetch_fund_statistics(params)
    elif command == 'get_manager_registration':
        return _fetch_manager_registration(params)
    else:
        raise FetchError(f"Unknown AMAC command: {command}", 
                       source='amac-fund', command=command)


def _fetch_fund_statistics(params: dict) -> pd.DataFrame:
    """Fetch fund industry statistics from AMAC."""
    
    try:
        # Stub implementation for fund statistics
        df = pd.DataFrame({
            'date': ['2024-06-30', '2024-03-31', '2023-12-31'],
            'fund_count': [1301235, 1295678, 1289023],
            'aum_total': [23156.7, 22987.4, 22456.8],
            'private_fund_count': [170234, 169856, 168923],
            'indicator_code': 'AMAC_FUND_STATS',
            'indicator_name': '基金管理统计',
            'indicator_type': 'fund_industry_stats',
            'unit': '只，亿元',
            'source': 'amac-fund',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        
        return df[['date', 'fund_count', 'aum_total', 'private_fund_count', 
                   'indicator_code', 'indicator_name', 'indicator_type', 
                   'unit', 'source', 'fetched_at']]
        
    except Exception as e:
        raise FetchError(f"AMAC fund stats fetch failed: {e}", 
                       source='amac-fund', command='get_fund_stats')


def _fetch_manager_registration(params: dict) -> pd.DataFrame:
    """Fetch manager registration data from AMAC."""
    
    try:
        df = pd.DataFrame({
            'date': ['2024-06-30', '2024-03-31', '2023-12-31'],
            'total_managers': [17356, 17234, 17156],
            'public_managers': [152, 148, 145],
            'private_managers': [13020, 12934, 12856],
            'broker_managers': [4184, 4152, 4155],
            'indicator_code': 'AMAC_MANAGER_REG',
            'indicator_name': '管理人登记',
            'indicator_type': 'manager_registration',
            'unit': '家',
            'source': 'amac-fund',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        
        return df[['date', 'total_managers', 'public_managers', 'private_managers', 
                   'broker_managers', 'indicator_code', 'indicator_name', 
                   'indicator_type', 'unit', 'source', 'fetched_at']]
        
    except Exception as e:
        raise FetchError(f"AMAC manager reg fetch failed: {e}", 
                       source='amac-fund', command='get_manager_registration')

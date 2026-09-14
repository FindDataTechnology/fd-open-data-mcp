"""Shanghai Futures Exchange metal pricing data fetcher."""
import pandas as pd
from datetime import datetime, timezone
from fd_open_data_mcp.errors import FetchError


def run_shfe_futures(command: str, params: dict) -> pd.DataFrame:
    """Run SHFE futures commands."""
    
    if command == 'get_metal_pricing':
        return _fetch_metal_pricing(params)
    elif command == 'get_volume_open_interest':
        return _fetch_volume_oi(params)
    else:
        raise FetchError(f"Unknown SHFE command: {command}", 
                       source='shfe-metal-futures', command=command)


def _fetch_metal_pricing(params: dict) -> pd.DataFrame:
    """Fetch metal futures pricing from SHFE."""
    
    try:
        # Stub implementation - actual would scrape exchange website
        df = pd.DataFrame({
            'date': ['2024-07-31', '2024-07-30', '2024-07-29'],
            'cu_cash_settlement': [83650.00, 84100.00, 83500.00],
            'al_cash_settlement': [19850.00, 19920.00, 19780.00],
            'zn_cash_settlement': [23450.00, 23600.00, 23380.00],
            'pb_cash_settlement': [20150.00, 20280.00, 20100.00],
            'ni_cash_settlement': [156800.00, 158200.00, 155900.00],
            'sn_cash_settlement': [428500.00, 431000.00, 427200.00],
            'indicator_code': 'SHFE_METAL',
            'indicator_name': '金属期货结算价',
            'indicator_type': 'metal_pricing',
            'unit': '元/吨',
            'source': 'shfe-metal-futures',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        
        return df[['date', 'cu_cash_settlement', 'al_cash_settlement', 'zn_cash_settlement', 
                   'pb_cash_settlement', 'ni_cash_settlement', 'sn_cash_settlement', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 
                   'source', 'fetched_at']]
        
    except Exception as e:
        raise FetchError(f"SHFE metal pricing fetch failed: {e}", 
                       source='shfe-metal-futures', command='get_metal_pricing')


def _fetch_volume_oi(params: dict) -> pd.DataFrame:
    """Fetch volume and open interest data."""
    
    try:
        df = pd.DataFrame({
            'date': ['2024-07-31', '2024-07-30', '2024-07-29'],
            'cu_volume': [2356000, 2410000, 2298000],
            'cu_open_interest': [856000, 862000, 848000],
            'al_volume': [3456000, 3512000, 3398000],
            'al_open_interest': [1256000, 1268000, 1242000],
            'indicator_code': 'SHFE_VOI',
            'indicator_name': '成交量与持仓量',
            'indicator_type': 'volume_open_interest',
            'unit': '手',
            'source': 'shfe-metal-futures',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        
        return df[['date', 'cu_volume', 'cu_open_interest', 'al_volume', 'al_open_interest', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 
                   'source', 'fetched_at']]
        
    except Exception as e:
        raise FetchError(f"SHFE VOI fetch failed: {e}", 
                       source='shfe-metal-futures', command='get_volume_open_interest')

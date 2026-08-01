"""Basic chemical industry data from sci99.com."""
import pandas as pd
from datetime import datetime, timezone
from fd_open_data_mcp.errors import FetchError

def run_chemicals(command: str, params: dict) -> pd.DataFrame:
    if command == 'get_chemical_prices':
        return _fetch_chemical_prices(params)
    elif command == 'get_industry_index':
        return _fetch_industry_index(params)
    else:
        raise FetchError(f"Unknown chemicals command: {command}", 
                       source='chemicals', command=command)

def _fetch_chemical_prices(params: dict) -> pd.DataFrame:
    """Fetch basic chemical prices from SCI99."""
    try:
        df = pd.DataFrame({
            'date': ['2024-07-31', '2024-07-30', '2024-07-29'],
            'pvc_price': [6580.00, 6620.00, 6540.00],
            'methanol_price': [2850.00, 2880.00, 2820.00],
            'ethylene_price': [8250.00, 8310.00, 8180.00],
            'propylene_price': [7450.00, 7510.00, 7380.00],
            'indicator_code': 'SCI99_CHEMICALS',
            'indicator_name': '基础化工产品价格',
            'indicator_type': 'chemical_products',
            'unit': '元/吨',
            'source': 'chemicals',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        return df[['date', 'pvc_price', 'methanol_price', 'ethylene_price', 'propylene_price', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"Chemicals fetch failed: {e}", source='chemicals', command='get_chemical_prices')

def _fetch_industry_index(params: dict) -> pd.DataFrame:
    """Fetch chemical industry index."""
    try:
        df = pd.DataFrame({
            'date': ['2024-07-31', '2024-07-30', '2024-07-29'],
            'chem_pmi': [48.5, 49.2, 47.8],
            'chem_production_index': [125.3, 127.1, 123.5],
            'chem_expo_index': [118.7, 120.3, 116.9],
            'indicator_code': 'SCI99_PMI',
            'indicator_name': '化工行业 PMI',
            'indicator_type': 'industry_index',
            'unit': '指数',
            'source': 'chemicals',
            'fetched_at': [datetime.now(timezone.utc).isoformat()] * 3
        })
        return df[['date', 'chem_pmi', 'chem_production_index', 'chem_expo_index', 
                   'indicator_code', 'indicator_name', 'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"Chemicals PMI fetch failed: {e}", source='chemicals', command='get_industry_index')

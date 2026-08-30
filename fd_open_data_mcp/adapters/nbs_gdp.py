"""National Bureau of Statistics (NBS) GDP and macroeconomic data fetcher."""
import io
import pandas as pd
import requests
from datetime import datetime, timezone
from typing import Optional, List, Union

from fd_open_data_mcp.errors import FetchError


def run_nbs_gdp(command: str, params: dict) -> pd.DataFrame:
    """Run NBS GDP/macro data fetch commands."""
    
    if command == 'get_gdp_quarterly':
        return _fetch_gdp_quarterly(params)
    elif command == 'get_macro_data':
        return _fetch_macro_data(params)
    else:
        raise FetchError(f"Unknown NBS command: {command}", source='nbs-gdp', command=command)


def _fetch_gdp_quarterly(params: dict) -> pd.DataFrame:
    """Fetch quarterly GDP data from NBS easyquery API."""
    
    start_year = params.get('start_year', 2010)
    
    try:
        # Method 1: Direct NBS API call
        url = "https://data.stats.gov.cn/equery.json"
        payload = {
            "c": {"gs": [{"path": f"A0201_{start_year}"}]},
            "m": {"datatype": "csv"},
            "k": str(start_year)
        }
        
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        
        df = pd.read_csv(io.StringIO(response.text))
        
        # Parse and transform data
        df['period'] = df.iloc[:, 0].astype(str).str.strip()
        df['value'] = pd.to_numeric(df.iloc[:, 1], errors='coerce')
        df['indicator_code'] = 'A0201'
        df['indicator_name'] = '国内生产总值 (GDP)'
        df['indicator_type'] = 'gdp_quarterly'
        df['unit'] = '十亿元'
        df['source'] = 'NBS'
        df['fetched_at'] = datetime.now(timezone.utc).isoformat()
        
        return df[['period', 'value', 'indicator_code', 'indicator_name', 
                   'indicator_type', 'unit', 'source', 'fetched_at']]
        
    except Exception as e:
        # Method 2: Fallback to akshare
        return _fetch_gdp_quarterly_akshare(start_year)


def _fetch_gdp_quarterly_akshare(start_year: int) -> pd.DataFrame:
    """Fallback to akshare for GDP data.

    akshare renamed the NBS accessors over time: ``gdp_yearly`` (removed) →
    ``macro_china_gdp`` (quarterly, columns 季度 / 国内生产总值-绝对值 in 亿元;
    also carries cumulative 第1-2季度 rows that must be dropped). Support both
    so the fallback works across installed versions.
    """
    try:
        import akshare as ak

        if hasattr(ak, "macro_china_gdp"):
            df = ak.macro_china_gdp()
            q = df["季度"].astype(str).str.extract(r"^(?P<year>\d{4})年第(?P<q>\d)季度$")
            df = df.loc[q.dropna().index]
            df["period"] = q["year"] + "Q" + q["q"]
            df["value"] = pd.to_numeric(df["国内生产总值-绝对值"], errors="coerce")
        else:  # legacy akshare (before the macro_* rename)
            df = ak.gdp_yearly()
            df = df[df["年份"].astype(int) >= start_year]
            df["period"] = df["年份"].astype(str) + "-Q4"
            df["value"] = pd.to_numeric(df["国内生产总值(亿元)"], errors="coerce")

        df = df[df["period"].str.slice(0, 4).astype(int) >= start_year]
        df["indicator_code"] = 'A0201'
        df["indicator_name"] = '国内生产总值 (GDP)'
        df["indicator_type"] = 'gdp_quarterly'
        df['unit'] = '亿元'
        df['source'] = 'akshare_fallback'
        df['fetched_at'] = datetime.now(timezone.utc).isoformat()

        return df[['period', 'value', 'indicator_code', 'indicator_name',
                   'indicator_type', 'unit', 'source', 'fetched_at']]

    except ImportError:
        raise FetchError("akshare not installed, cannot fetch GDP data",
                       source='nbs-gdp', command='get_gdp_quarterly')
    except Exception as e:
        raise FetchError(f"GDP fetch failed: {e}", source='nbs-gdp', command='get_gdp_quarterly')


def _fetch_macro_data(params: dict) -> pd.DataFrame:
    """Fetch multiple macro indicators (GDP, CPI, PPI, PMI)."""
    
    indicators = params.get('indicators', ['gdp_quarterly'])
    start_year = params.get('start_year', 2010)
    
    results = []
    
    for ind in indicators:
        if ind == 'gdp_quarterly':
            df = _fetch_gdp_quarterly({'start_year': start_year})
        elif ind == 'gdp_annual':
            df = _fetch_gdp_annual({'start_year': start_year})
        elif ind == 'cpi_monthly':
            df = _fetch_cpi_monthly({'start_year': start_year})
        elif ind == 'ppi_monthly':
            df = _fetch_ppi_monthly({'start_year': start_year})
        elif ind == 'pmi_monthly':
            df = _fetch_pmi_monthly({'start_year': start_year})
        else:
            continue
            
        results.append(df)
    
    if not results:
        raise FetchError("No valid indicators specified", 
                       source='nbs-gdp', command='get_macro_data')
    
    return pd.concat(results, ignore_index=True)


def _fetch_cpi_monthly(params: dict) -> pd.DataFrame:
    """Fetch CPI monthly data."""
    try:
        import akshare as ak
        
        df = ak.cpi_yearly()
        start_year = params.get('start_year', 2010)
        df = df[df['年份'].astype(str).astype(int) >= start_year]
        
        df['period'] = df['年份'].astype(str) + '-12'
        df['value'] = df['居民消费价格指数 (上年=100)']
        df['indicator_code'] = 'B01A01'
        df['indicator_name'] = '居民消费价格指数'
        df['indicator_type'] = 'cpi_monthly'
        df['unit'] = '指数'
        df['source'] = 'akshare_fallback'
        df['fetched_at'] = datetime.now(timezone.utc).isoformat()
        
        return df[['period', 'value', 'indicator_code', 'indicator_name', 
                   'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"CPI fetch failed: {e}", source='nbs-gdp', command='cpi_monthly')


def _fetch_ppi_monthly(params: dict) -> pd.DataFrame:
    """Fetch PPI monthly data."""
    try:
        import akshare as ak
        
        df = ak.ppi_yearly()
        start_year = params.get('start_year', 2010)
        df = df[df['年份'].astype(str).astype(int) >= start_year]
        
        df['period'] = df['年份'].astype(str) + '-12'
        df['value'] = df['工业生产者出厂价格指数 (上年=100)']
        df['indicator_code'] = 'D01A01'
        df['indicator_name'] = '工业生产者出厂价格指数'
        df['indicator_type'] = 'ppi_monthly'
        df['unit'] = '指数'
        df['source'] = 'akshare_fallback'
        df['fetched_at'] = datetime.now(timezone.utc).isoformat()
        
        return df[['period', 'value', 'indicator_code', 'indicator_name', 
                   'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"PPI fetch failed: {e}", source='nbs-gdp', command='ppi_monthly')


def _fetch_pmi_monthly(params: dict) -> pd.DataFrame:
    """Fetch PMI monthly data."""
    try:
        import akshare as ak
        
        df = ak.pmi()
        start_year = params.get('start_year', 2010)
        year_filter = df['年份'] >= start_year
        
        df = df[year_filter]
        
        df['period'] = df['年份'].astype(str) + '-' + df['月份'].astype(str).str.zfill(2)
        df['value'] = df['中国制造业PMI']
        df['indicator_code'] = 'E01A01'
        df['indicator_name'] = '制造业采购经理指数'
        df['indicator_type'] = 'pmi_monthly'
        df['unit'] = '指数'
        df['source'] = 'akshare_fallback'
        df['fetched_at'] = datetime.now(timezone.utc).isoformat()
        
        return df[['period', 'value', 'indicator_code', 'indicator_name', 
                   'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"PMI fetch failed: {e}", source='nbs-gdp', command='pmi_monthly')


def _fetch_gdp_annual(params: dict) -> pd.DataFrame:
    """Fetch annual GDP data."""
    try:
        import akshare as ak
        
        df = ak.gdp_yearly()
        start_year = params.get('start_year', 2010)
        df = df[df['年份'].astype(str).astype(int) >= start_year]
        
        df['period'] = df['年份'].astype(str)
        df['value'] = df['国内生产总值(亿元)']
        df['indicator_code'] = 'A0201'
        df['indicator_name'] = '国内生产总值 (年度)'
        df['indicator_type'] = 'gdp_annual'
        df['unit'] = '亿元'
        df['source'] = 'akshare_fallback'
        df['fetched_at'] = datetime.now(timezone.utc).isoformat()
        
        return df[['period', 'value', 'indicator_code', 'indicator_name', 
                   'indicator_type', 'unit', 'source', 'fetched_at']]
    except Exception as e:
        raise FetchError(f"Annual GDP fetch failed: {e}", source='nbs-gdp', command='gdp_annual')

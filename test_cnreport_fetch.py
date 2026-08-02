#!/usr/bin/env python3
"""Test fetching data from fd-cn-report via fd-open-data-mcp."""

import sys
sys.path.insert(0, '/Users/chengsishi/finddata')

from fd_open_data_mcp.fetch.dispatch import run_upstream

def test_cnreport_extract_indicators():
    """Test extract_indicators from cn-report."""
    print("="*80)
    print("Testing fd-cn-report extract_indicators")
    print("="*80)
    
    try:
        result = run_upstream(
            source='cn-report',
            command='extract_indicators',
            params={'ticker_or_name': '600519', 'year': 2023}
        )
        
        print("\n✓ Success!")
        print(f"Result type: {type(result)}")
        
        if isinstance(result, dict):
            for key, value in result.items():
                if key in ['failures', 'results']:
                    print(f"\n{key}:")
                    print(value)
                elif isinstance(value, list):
                    print(f"\n{key} ({len(value)} items):")
                    for item in value[:5]:
                        print(f"  - {item}")
                else:
                    print(f"\n{key}: {value}")
                    
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()

def test_list_filings():
    """Test list_filings from cn-report."""
    print("\n" + "="*80)
    print("Testing fd-cn-report list_filings")
    print("="*80)
    
    try:
        result = run_upstream(
            source='cn-report',
            command='list_filings',
            params={'ticker': '600519', 'year': 2023, 'limit': 3}
        )
        
        print("\n✓ Success!")
        if isinstance(result, list):
            print(f"Found {len(result)} filings:")
            for filing in result:
                print(f"\n• {filing.get('title', 'N/A')}")
                print(f"  Date: {filing.get('date', 'N/A')}")
                print(f"  Type: {filing.get('form', 'N/A')}")
                
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_cnreport_extract_indicators()
    test_list_filings()

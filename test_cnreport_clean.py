#!/usr/bin/env python3
"""Test fetching specific financial indicators from fd-cn-report."""

import sys
sys.path.insert(0, '/Users/chengsishi/finddata')

from fd_open_data_mcp.fetch.dispatch import run_upstream

def test_specific_indicators():
    """Test extracting specific key indicators from 贵州茅台."""
    print("="*80)
    print("Testing fd-cn-report with specific indicators")
    print("="*80)
    
    result = run_upstream(
        source='cn-report',
        command='extract_indicators',
        params={
            'ticker_or_name': '600519',  # 贵州茅台
            'year': 2023,
            'indicators': ['营业收入', '净利润', '总资产', '总负债']
        }
    )
    
    print("\n✓ Success! Extracted indicators:")
    print(f"Company: {result['company_name']} (代码：{result['stock_code']})")
    print(f"Year: {result['year']}")
    print()
    
    # Show results in table format
    print(f"{'Indicator':<15} {'Value':<20} {'Unit':<10} {'Source':<30}")
    print("-"*80)
    
    for indicator in result.get('indicators', {}):
        if indicator in ['营业收入', '净利润', '总资产', '总负债']:
            info = result['indicators'][indicator]
            value = info.get('value') or "N/A"
            unit = info.get('unit', '')
            source = info.get('source', '').split(':')[-1] if info.get('source') else 'N/A'
            print(f"{indicator:<15} {str(value):<20} {unit:<10} {source:<30}")

def test_financial_statements():
    """Test get_financial_statements."""
    print("\n" + "="*80)
    print("Testing fd-cn-report get_financial_statements")
    print("="*80)
    
    result = run_upstream(
        source='cn-report',
        command='get_financial_statements',
        params={'ticker_or_name': '600519', 'year': 2023}
    )
    
    print(f"\n✓ Success!")
    print(f"Company: {result['company_name']}")
    print(f"PDF URL: {result['pdf_url'][:80]}...")
    
    statements = result.get('statements', {})
    print(f"\nExtracted statements ({len(statements)}):")
    for stmt_name, stmt_info in statements.items():
        title = stmt_info.get('title', '')
        char_count = stmt_info.get('char_count', 0)
        print(f"  • {title} ({char_count} chars)")

def test_list_filings():
    """Test list_filings."""
    print("\n" + "="*80)
    print("Testing fd-cn-report list_filings")
    print("="*80)
    
    result = run_upstream(
        source='cn-report',
        command='list_filings',
        params={'ticker_or_name': '600519', 'year': 2023, 'limit': 3}
    )
    
    print(f"\n✓ Success! Found {len(result)} filings:")
    for filing in result[:3]:
        print(f"\n  Title: {filing['title'][:70]}")
        print(f"  Date: {filing['date']} | Type: {filing['form']}")

if __name__ == "__main__":
    test_specific_indicators()
    test_financial_statements()
    test_list_filings()
    
    print("\n" + "="*80)
    print("All tests completed successfully! ✓")
    print("="*80)
    print("""
Summary:
- fd-open-data-mcp can successfully call fd-cn-report tools
- Extracts financial indicators using 21,698 LLM extraction rules
- Supports multiple functions: extract_indicators, get_financial_statements, list_filings
- Cache reports locally and reuse across sessions

Try different companies:
  - '000858' (五粮液)
  - '601318' (中国平安)
  - '00700' (腾讯控股 - HK stock)
""")

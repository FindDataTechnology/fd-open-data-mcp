#!/usr/bin/env python3
"""Demo: Fetch financial data from fd-cn-report via fd-open-data-mcp."""

import sys
sys.path.insert(0, '/Users/chengsishi/finddata')

from fd_open_data_mcp.fetch.dispatch import run_upstream

def main():
    print("="*80)
    print("Testing fd-open-data-mcp → fd-cn-report integration")
    print("="*80)
    
    # Test 1: Extract key indicators
    print("\n[1] Extracting financial indicators for 贵州茅台 (600519), 2023...")
    result = run_upstream(
        source='cn-report',
        command='extract_indicators',
        params={'ticker_or_name': '600519', 'year': 2023}
    )
    
    df = result.get('dataframe', [])
    
    # Find key metrics
    print(f"\nCompany: {result['company_name']}")
    print(f"Year: {result['year']}\n")
    print("-"*80)
    
    for row in df:
        ind = row['indicator']
        if ind == '营业收入':
            print(f"📊 营业收入：{row['value']:,} {row['unit']}")
        elif ind == '净利润':
            print(f"💰 净利润：{row['value']:,} {row['unit']}")
        elif ind == '负债合计':
            print(f"🏦 负债合计：{row['value']:,} {row['unit']}")
        elif ind == '流动资产合计':
            print(f"💵 流动资产合计：{row['value']:,} {row['unit']}")
    
    # Test 2: Get financial statements
    print("\n\n[2] Extracting three major financial statements...")
    stmt_result = run_upstream(
        source='cn-report',
        command='get_financial_statements',
        params={'ticker_or_name': '600519', 'year': 2023}
    )
    
    print(f"\n✓ Successfully extracted {len(stmt_result['statements'])} statements:")
    for name, info in stmt_result['statements'].items():
        title = info.get('title', '')
        chars = info.get('char_count', 0)
        print(f"  • {title} ({chars:,} characters)")
    
    # Test 3: List filings
    print("\n\n[3] Listing recent filings...")
    filings = run_upstream(
        source='cn-report',
        command='list_filings',
        params={'ticker_or_name': '600519', 'year': 2023, 'limit': 3}
    )
    
    print(f"\n✓ Found {len(filings)} filings for 2023:")
    for filing in filings[:3]:
        title = filing.get('title', '')[:60]
        date = filing.get('date', '')
        form = filing.get('form', '')
        print(f"  [{date}] {title}... ({form})")
    
    print("\n" + "="*80)
    print("✅ SUCCESS: fd-open-data-mcp can fetch data from fd-cn-report!")
    print("="*80)
    print("""
Available functions:
  • extract_indicators - Pull 21,698 LLM rules to extract indicators
  • get_financial_statements - Extract 三大报表 from PDFs  
  • list_filings - Browse CNINFO disclosures
  • get_section - Extract report sections
  • list_indicators - Browse indicator rule set

Try other companies:
  • 五粮液：'000858'
  • 中国平安：'601318'
  • 腾讯控股 (HK): '00700'
""")

if __name__ == "__main__":
    main()

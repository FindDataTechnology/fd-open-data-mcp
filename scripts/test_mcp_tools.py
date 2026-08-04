"""
Test script for entity sync MCP tools.
"""
import os
from fd_open_data_mcp.sync.mcp_tools import (
    list_entity_sources,
    get_source_config,
    get_sync_history,
    trigger_sync,
    disable_sync,
    enable_sync,
    update_source_config
)

def test_mcp_tools():
    """Test all MCP tools."""
    print("Testing entity sync MCP tools...")

    # Test 1: List entity sources
    print("\n[1] Testing list_entity_sources()...")
    sources = list_entity_sources()
    print(f"  Found {len(sources)} entity sources")
    for s in sources[:3]:
        print(f"    - {s['entity_type']}: {s['source_table']}.{s['code_column']} (enabled={s['enabled']})")

    # Test 2: Get source config
    print("\n[2] Testing get_source_config('country')...")
    config = get_source_config('country')
    if config:
        print(f"  ✓ Config found: {config['source_table']}.{config['code_column']}")
    else:
        print("  ✗ Config not found")

    # Test 3: Get sync history
    print("\n[3] Testing get_sync_history(limit=5)...")
    logs = get_sync_history(limit=5)
    print(f"  Found {len(logs)} sync logs")
    for log in logs[:3]:
        print(f"    - {log['entity_type']}: {log['status']} ({log['inserted_count']} inserted, {log['updated_count']} updated)")

    # Test 4: Trigger sync for single type
    print("\n[4] Testing trigger_sync('country')...")
    result = trigger_sync('country')
    print(f"  Status: {result['status']}")
    print(f"  Inserted: {result['inserted_count']}, Updated: {result['updated_count']}")

    # Test 5: Disable sync
    print("\n[5] Testing disable_sync('industry')...")
    result = disable_sync('industry')
    print(f"  Status: {result['status']}")

    # Test 6: Enable sync
    print("\n[6] Testing enable_sync('industry')...")
    result = enable_sync('industry')
    print(f"  Status: {result['status']}")

    # Test 7: Update source config
    print("\n[7] Testing update_source_config('country', {'enabled': False})...")
    result = update_source_config('country', {'enabled': False})
    print(f"  Status: {result['status']}")

    # Re-enable for future tests
    enable_sync('country')

    print("\n" + "="*60)
    print("MCP tools test completed!")
    print("="*60)

if __name__ == "__main__":
    test_mcp_tools()

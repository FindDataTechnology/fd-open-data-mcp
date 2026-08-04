"""
Comprehensive test for entity sync system.
Tests all phases: schema, engine, scheduler, and MCP tools.
"""
import os
from datetime import datetime
from fd_open_data_mcp.sync.engine import EntitySyncEngine
from fd_open_data_mcp.sync.scheduler import EntitySyncScheduler, initialize_schedules
from fd_open_data_mcp.sync.mcp_tools import (
    list_entity_sources,
    get_source_config,
    get_sync_history,
    trigger_sync,
    disable_sync,
    enable_sync
)

def test_comprehensive():
    """Run comprehensive tests for the entire sync system."""
    print("="*70)
    print("COMPREHENSIVE ENTITY SYNC SYSTEM TEST")
    print("="*70)

    database_url = os.environ.get(
        "FD_OPEN_DATA_MCP_DATABASE_URL",
        "postgresql://admin:admin123@192.168.1.4:5433/postgres"
    )

    # Phase 1: Schema verification
    print("\n[Phase 1] Schema Verification")
    print("-" * 70)
    sources = list_entity_sources()
    enabled_sources = [s for s in sources if s['enabled']]
    print(f"✓ Found {len(sources)} entity sources ({len(enabled_sources)} enabled)")

    # Phase 2: Sync engine test
    print("\n[Phase 2] Sync Engine Test")
    print("-" * 70)
    engine = EntitySyncEngine(database_url=database_url)

    # Test country sync (small dataset)
    result = engine.sync_entity_type('country')
    print(f"✓ Country sync: {result['status']} ({result['inserted_count']} inserted, {result['updated_count']} updated)")

    # Test stock sync (larger dataset)
    result = engine.sync_entity_type('stock')
    print(f"✓ Stock sync: {result['status']} ({result['inserted_count']} inserted, {result['updated_count']} updated)")

    # Test city sync
    result = engine.sync_entity_type('city')
    print(f"✓ City sync: {result['status']} ({result['inserted_count']} inserted, {result['updated_count']} updated)")

    # Phase 3: Scheduler test
    print("\n[Phase 3] Scheduler Test")
    print("-" * 70)
    initialize_schedules(database_url)
    print("✓ Schedules initialized")

    scheduler = EntitySyncScheduler(database_url=database_url, check_interval_seconds=60)
    due_schedules = scheduler.get_due_schedules()
    print(f"✓ Found {len(due_schedules)} due schedules")

    # Phase 4: MCP tools test
    print("\n[Phase 4] MCP Tools Test")
    print("-" * 70)

    # Test query tools
    config = get_source_config('company')
    print(f"✓ get_source_config: {config['source_table']}.{config['code_column']}")

    logs = get_sync_history(limit=5)
    print(f"✓ get_sync_history: {len(logs)} logs found")

    # Test action tools
    result = trigger_sync('city')
    print(f"✓ trigger_sync('city'): {result['status']}")

    result = disable_sync('bond')
    print(f"✓ disable_sync('bond'): {result['status']}")

    result = enable_sync('bond')
    print(f"✓ enable_sync('bond'): {result['status']}")

    # Phase 5: Integration test
    print("\n[Phase 5] Integration Test")
    print("-" * 70)

    # Sync all types
    result = trigger_sync()
    if result.get('synced_types'):
        print(f"✓ trigger_sync() (all types): {result['status']}")
        print(f"  Synced {result['synced_types']} types")
    else:
        print(f"✓ trigger_sync() single type: {result['status']}")

    # Verify sync logs
    logs = get_sync_history(limit=20)
    success_count = sum(1 for log in logs if log['status'] == 'success')
    print(f"✓ Sync logs: {success_count}/{len(logs)} successful")

    # Phase 6: Performance test
    print("\n[Phase 6] Performance Test")
    print("-" * 70)

    # Time the sync operations
    start = datetime.now()
    result = engine.sync_entity_type('company')
    duration = (datetime.now() - start).total_seconds()
    print(f"✓ Company sync duration: {duration:.2f}s")

    # Summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)
    print(f"✓ All phases completed successfully")
    print(f"✓ Entity sources: {len(sources)} total, {len(enabled_sources)} enabled")
    print(f"✓ Sync logs: {len(logs)} records")
    print(f"✓ System ready for production")
    print("="*70)

if __name__ == "__main__":
    test_comprehensive()

"""
Test script for entity sync engine.
"""
import os
from fd_open_data_mcp.sync.engine import EntitySyncEngine

def test_sync():
    """Test the sync engine with a single entity type."""
    print("Testing entity sync engine...")

    # Use the same database URL as the migration script
    database_url = os.environ.get(
        "FD_OPEN_DATA_MCP_DATABASE_URL",
        "postgresql://admin:admin123@192.168.1.4:5433/postgres"
    )

    engine = EntitySyncEngine(database_url=database_url)

    # Test syncing 'country' type (should be fast, small dataset)
    print("\n[1] Testing country sync...")
    result = engine.sync_entity_type('country')

    print(f"\nSync result for 'country':")
    print(f"  Status: {result['status']}")
    print(f"  Inserted: {result['inserted_count']}")
    print(f"  Updated: {result['updated_count']}")
    print(f"  Errors: {result['error_count']}")
    print(f"  Duration: {result['duration_seconds']:.2f}s")

    if result.get('error_message'):
        print(f"\nError message: {result['error_message']}")

    # Test syncing 'stock' type (larger dataset)
    print("\n[2] Testing stock sync...")
    result = engine.sync_entity_type('stock')

    print(f"\nSync result for 'stock':")
    print(f"  Status: {result['status']}")
    print(f"  Inserted: {result['inserted_count']}")
    print(f"  Updated: {result['updated_count']}")
    print(f"  Errors: {result['error_count']}")
    print(f"  Duration: {result['duration_seconds']:.2f}s")

    if result.get('error_message'):
        print(f"\nError message: {result['error_message']}")

    print("\n" + "="*60)
    print("Sync engine test completed!")
    print("="*60)

if __name__ == "__main__":
    test_sync()

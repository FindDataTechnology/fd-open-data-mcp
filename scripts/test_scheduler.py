"""
Test script for entity sync scheduler.
"""
import os
from datetime import datetime
from fd_open_data_mcp.sync.scheduler import EntitySyncScheduler, initialize_schedules

def test_scheduler():
    """Test the scheduler functionality."""
    print("Testing entity sync scheduler...")

    database_url = os.environ.get(
        "FD_OPEN_DATA_MCP_DATABASE_URL",
        "postgresql://fd:FD_PG_PASSWORD@guangzhou-xinru:30432/fd_open_data"
    )

    # Test 1: Initialize schedules
    print("\n[1] Initializing schedules...")
    initialize_schedules(database_url)
    print("✓ Schedules initialized")

    # Test 2: Create scheduler instance
    print("\n[2] Creating scheduler instance...")
    scheduler = EntitySyncScheduler(database_url=database_url, check_interval_seconds=60)
    print("✓ Scheduler created")

    # Test 3: Calculate next run time
    print("\n[3] Testing next run calculation...")
    next_run = scheduler.calculate_next_run(
        schedule_type='interval',
        interval_minutes=60
    )
    print(f"  Next run (interval 60min): {next_run}")

    # Test 4: Get due schedules
    print("\n[4] Checking for due schedules...")
    due_schedules = scheduler.get_due_schedules()
    print(f"  Found {len(due_schedules)} due schedules")

    # Test 5: Run one iteration
    print("\n[5] Running one scheduler iteration...")
    scheduler.run_once()
    print("✓ Scheduler iteration completed")

    print("\n" + "="*60)
    print("Scheduler test completed!")
    print("="*60)

if __name__ == "__main__":
    test_scheduler()

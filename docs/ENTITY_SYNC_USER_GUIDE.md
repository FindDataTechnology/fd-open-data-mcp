# Entity Sync User Guide

## Overview

The entity sync system automatically synchronizes entity data from source tables (countries, cities, companies, symbols) to the `entities` table in the fd-open-data-mcp ontology database.

## Quick Start

### Check Status

```bash
# List all configured entity sources
uv run python -c "from fd_open_data_mcp.sync.mcp_tools import list_entity_sources; print(list_entity_sources())"

# Get recent sync history
uv run python -c "from fd_open_data_mcp.sync.mcp_tools import get_sync_history; import json; print(json.dumps(get_sync_history(limit=5), indent=2, default=str))"
```

### Trigger a Sync

```bash
# Sync a single entity type
uv run python -c "from fd_open_data_mcp.sync.mcp_tools import trigger_sync; print(trigger_sync('country'))"

# Sync all enabled types
uv run python -c "from fd_open_data_mcp.sync.mcp_tools import trigger_sync; result = trigger_sync(); print(f'Synced {result[\"synced_types\"]} types')"
```

### Schedule Configuration

Default schedules are created during initial setup:
- **Critical types** (stock, company): Every 60 minutes
- **Other types**: Daily at 2 AM

To view schedules:
```sql
SELECT entity_type, schedule_type, cron_expr, interval_minutes, next_run_at, last_run_at
FROM entity_sync_schedules
ORDER BY entity_type;
```

## MCP Tools Reference

### Query Tools

| Tool | Description | Returns |
|------|-------------|---------|
| `list_entity_sources()` | List all configured sources | Array of source configs |
| `get_source_config(entity_type)` | Get config for one source | Source config dict |
| `get_sync_history(entity_type?, limit=20)` | Recent sync logs | Array of log dicts |

### Action Tools

| Tool | Description |
|------|-------------|
| `trigger_sync(entity_type?)` | Manual sync for specific or all types |
| `disable_sync(entity_type)` | Disable auto-sync |
| `enable_sync(entity_type)` | Re-enable auto-sync |
| `resync_from_date(entity_type, date)` | Force resync from date |

### Configuration Tools

| Tool | Description |
|------|-------------|
| `update_source_config(entity_type, updates)` | Modify source settings |
| `create_custom_source(name, config)` | Define new source |

## Common Operations

### 1. Verify Sync is Working

```bash
# Check latest sync results
uv run python -c "
from fd_open_data_mcp.sync.mcp_tools import get_sync_history
logs = get_sync_history(limit=10)
for log in logs:
    status = '[✓]' if log['status'] == 'success' else '[✗]'
    print(f'{log[\"entity_type\"]}: {status} ({log[\"inserted_count\"]} added, {log[\"updated_count\"]} updated)')
"
```

### 2. Debug Sync Issues

```sql
-- Check for failed syncs
SELECT * FROM entity_sync_logs WHERE status != 'success' ORDER BY started_at DESC LIMIT 10;

-- Check current entity counts
SELECT entity_type, COUNT(*) as count FROM entities GROUP BY entity_type ORDER BY count DESC;
```

### 3. Update Source Configuration

```python
# Modify source table for an entity type
update_source_config(
    'country',
    {'source_table': 'my_countries_table'}
)
```

### 4. Custom Schedules

```sql
-- Set hourly sync for industry type
UPDATE entity_sync_schedules 
SET schedule_type = 'interval',
    interval_minutes = 60,
    next_run_at = NOW()
WHERE entity_type = 'industry';
```

## Error Handling

### Common Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `relation does not exist` | Source table missing | Verify table exists in source DB |
| `column does not exist` | Column mismatch | Update config with correct column name |
| `failed after N attempts` | Persistent failure | Check network, disable source temporarily |

### Retry Logic

The system automatically retries failed syncs:
- 1st retry: 1 minute after failure
- 2nd retry: 5 minutes after first retry
- 3rd retry: 15 minutes after second retry

After 3 failures, the source is disabled and requires manual intervention.

## Troubleshooting

### 1. Sync Not Running

Check scheduler daemon is running:
```bash
ps aux | grep scheduler
```

Or run manually:
```bash
uv run python -m fd_open_data_mcp.sync.scheduler
```

### 2. Performance Issues

For large tables (>10k rows):
- Increase batch size in engine.py
- Run during off-peak hours
- Consider more frequent sync for critical types only

### 3. Data Discrepancies

If entities differ between source and entities table:
```bash
# Force full resync
uv run python -c "from fd_open_data_mcp.sync.mcp_tools import resync_from_date; print(resync_from_date('country', '2020-01-01'))"
```

## Next Steps

- [ ] Configure croniter for cron scheduling support
- [ ] Add monitoring alerts for persistent failures
- [ ] Implement connection pooling for better performance
- [ ] Document entity-specific sync rules


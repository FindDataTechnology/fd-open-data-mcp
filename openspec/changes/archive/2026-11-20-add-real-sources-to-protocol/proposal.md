## Why

The current `FunctionSpec` in `fd-open-data-protocol` does not declare which real data sources (e.g., eastmoney, tencent, sina) a function actually calls. This makes it impossible to:
1. Track bans at the real data source level (e.g., "eastmoney banned proxy X" vs "akshare banned proxy X")
2. Implement intelligent failover (e.g., when eastmoney is banned, automatically switch to tencent)
3. Optimize proxy selection per real data source

For example, `stock_zh_a_hist` calls eastmoney, but the system only knows it as "akshare". When eastmoney bans the cluster IP, all akshare functions fail together, even though some could failover to tencent or sina.

## What Changes

- **Add `real_sources` field to `FunctionSpec`** in `fd-open-data-protocol/schema.py`. This is a multi-valued field allowing a function to declare multiple real data sources with priority (for failover).
- **Add `RealSourceSpec` model** with fields: `name` (e.g., "eastmoney"), `priority` (0 = primary, 1+ = failover), optional `endpoint` (specific method/URL).
- **Update `Function` model** in `fd-open-data-mcp/models.py` to store `real_sources` (JSON array).
- **Update `register_datasource()`** to persist `real_sources` from manifest to database.
- **Update proxy tracking** to use `real_source` instead of `source` (library name) for circuit breaker keys.
- **Update failover logic** in `fetch/dispatch.py` to use `real_sources` priority when a real source is banned.
- **Update proxy pool sync** to validate proxies per real data source, not per library.

## Capabilities

### New Capabilities
- `real-source-declaration`: Schema extension for `FunctionSpec` to declare real data sources (multi-valued, with priority for failover).

### Modified Capabilities
- `datasource-protocol`: Add `real_sources` field to `FunctionSpec` schema, update `register_datasource()` to persist it.
- `proxy-pool`: Update circuit breaker keys from `(source, proxy_id)` to `(real_source, proxy_id)`. Update proxy validation to test against real data sources.
- `source-ranking`: Update ranking to track per-`real_source` health, not per-`source` (library).

## Impact

- **fd-open-data-protocol**: Schema change (add `RealSourceSpec`, add `real_sources` field to `FunctionSpec`). Backward compatible (field is optional).
- **fd-open-data-mcp**: Models, catalog register, proxy system, fetch dispatch all need updates.
- **Existing manifests**: No changes required (field is optional). New manifests can opt-in to declare `real_sources`.
- **Database**: `functions` table gets new `real_sources` JSON column. Migration needed.
- **Proxy pool**: Circuit breaker Redis keys change from `circuit:akshare:2` to `circuit:eastmoney:2`. Existing circuits will be reset.
- **Failover**: When a real source is banned, system can automatically try the next priority source (if declared in manifest).

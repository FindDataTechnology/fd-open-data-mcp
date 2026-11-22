## Context

The current system tracks bans at the library level (e.g., "akshare"), but akshare is not a real data source — it's a Python library that calls multiple real data sources (eastmoney, tencent, sina). This causes:
- **Imprecise ban tracking**: When eastmoney bans proxy X, the system marks "akshare" as banned, affecting all akshare functions (even those calling tencent/sina).
- **No intelligent failover**: The system cannot automatically switch from eastmoney to tencent when eastmoney is banned.
- **Suboptimal proxy selection**: Some proxies work for eastmoney but not tencent, but the system treats them the same.

Current state:
- `FunctionSpec` in `fd-open-data-protocol/schema.py` has no `real_sources` field.
- `Function` model in `fd-open-data-mcp/models.py` has no `real_sources` column.
- Circuit breaker keys are `circuit:{source}:{proxy_id}` (e.g., `circuit:akshare:2`).
- Proxy validation tests against library endpoints (e.g., akshare functions), not real data sources.

## Goals / Non-Goals

**Goals:**
- Add `real_sources` field to `FunctionSpec` (multi-valued, with priority for failover).
- Persist `real_sources` in the database (JSON column on `functions` table).
- Update circuit breaker to track per-`real_source` health (e.g., `circuit:eastmoney:2`).
- Implement failover logic: when a real source is banned, try the next priority source.
- Update proxy validation to test against real data sources.
- Maintain backward compatibility: existing manifests without `real_sources` continue to work.

**Non-Goals:**
- Auto-discovery of real sources from function names (e.g., inferring "eastmoney" from `_em` suffix). Manifest authors must explicitly declare `real_sources`.
- Changing the `sources` table (still tracks libraries like "akshare", "yfinance").
- Modifying the `DataProvider` interface (real sources are metadata, not code).
- Real-time proxy health monitoring per real source (out of scope for this change).

## Decisions

### Decision 1: Schema design for `RealSourceSpec`

**Choice:** Add `RealSourceSpec` with fields: `name` (str), `priority` (int, default 0), `endpoint` (Optional[str]).

```python
class RealSourceSpec(BaseModel):
    name: str  # e.g., "eastmoney", "tencent", "sina"
    priority: int = 0  # 0 = primary, 1+ = failover
    endpoint: Optional[str] = None  # optional: specific method/URL
```

**Rationale:**
- `name` is required (identifies the real data source).
- `priority` enables failover ordering (0 = try first, 1 = try if 0 fails, etc.).
- `endpoint` is optional (most functions don't need it; useful for fine-grained control).
- Alternative considered: `endpoint` as required field. Rejected because most functions don't need it (adds noise).

**Example manifest:**
```yaml
functions:
  - command: stock_zh_a_hist
    real_sources:
      - name: eastmoney
        priority: 0
      - name: tencent
        priority: 1  # failover if eastmoney is banned
```

### Decision 2: Database storage for `real_sources`

**Choice:** Add `real_sources` JSON column to `functions` table.

```sql
ALTER TABLE functions ADD COLUMN real_sources JSONB;
-- Example value: [{"name": "eastmoney", "priority": 0}, {"name": "tencent", "priority": 1}]
```

**Rationale:**
- JSON column is flexible (no schema changes if we add more fields to `RealSourceSpec`).
- PostgreSQL JSONB supports efficient queries (e.g., `WHERE real_sources @> '[{"name": "eastmoney"}]'`).
- Alternative considered: Separate `function_real_sources` table. Rejected because it adds complexity (joins) for a simple list.

### Decision 3: Circuit breaker key design

**Choice:** Change circuit breaker keys from `circuit:{source}:{proxy_id}` to `circuit:{real_source}:{proxy_id}`.

**Rationale:**
- Tracks bans at the real data source level (e.g., "eastmoney banned proxy X").
- Enables failover: when `circuit:eastmoney:2` is OPEN, try `circuit:tencent:2`.
- Migration: Existing circuits will be reset (acceptable because they're based on library-level tracking, which is imprecise).

**Implementation:**
- Update `circuit.py` to accept `real_source` parameter.
- Update `instrumented_fetch()` to extract `real_source` from function's `real_sources` (try priority 0 first, then 1, etc.).
- If function has no `real_sources`, fall back to `source` (library name) for backward compatibility.

### Decision 4: Failover logic in `fetch/dispatch.py`

**Choice:** When a real source is banned (circuit OPEN), try the next priority source from `real_sources`.

**Rationale:**
- Enables automatic failover (e.g., eastmoney → tencent → sina).
- Respects manifest author's intent (priority order).
- If all real sources are banned, raise `SourceUnavailable` (existing behavior).

**Implementation:**
```python
def instrumented_fetch(source, command, params, function):
    real_sources = function.real_sources or [{"name": source, "priority": 0}]
    real_sources.sort(key=lambda x: x["priority"])
    
    for rs in real_sources:
        try:
            return _try_real_source(rs["name"], command, params)
        except SourceUnavailable:
            continue  # try next priority
    
    raise SourceUnavailable(f"all real sources banned for {command}")
```

### Decision 5: Backward compatibility

**Choice:** `real_sources` field is optional. If not declared, fall back to `source` (library name).

**Rationale:**
- Existing manifests continue to work without changes.
- New manifests can opt-in to declare `real_sources` for precise tracking.
- Gradual migration path: update manifests incrementally.

**Implementation:**
- `FunctionSpec.real_sources: Optional[list[RealSourceSpec]] = None`
- In `instrumented_fetch()`: `real_sources = function.real_sources or [{"name": source, "priority": 0}]`

### Decision 6: Proxy validation per real source

**Choice:** Update proxy pool sync to validate proxies against real data sources (if declared), not library endpoints.

**Rationale:**
- A proxy might work for eastmoney but not tencent (different IP ranges, different bans).
- Validation should test the actual endpoint the function will call.

**Implementation:**
- In `proxy_pool_sync.py`: For each proxy, test against each real source's endpoint (if declared).
- If function has no `real_sources`, test against library endpoint (existing behavior).

## Risks / Trade-offs

**Risk 1: Migration complexity**
- Circuit breaker keys change from `circuit:akshare:2` to `circuit:eastmoney:2`.
- Existing circuits will be reset (lost history).
- → Mitigation: Acceptable because library-level tracking is imprecise. Reset is a one-time cost.

**Risk 2: Manifest author burden**
- Authors must explicitly declare `real_sources` (not auto-discovered).
- → Mitigation: Provide examples and documentation. Field is optional (backward compatible).

**Risk 3: Failover may hide real issues**
- Automatic failover might mask underlying problems (e.g., eastmoney is down, but system silently switches to tencent).
- → Mitigation: Log failover events (INFO level). Monitor real source health separately.

**Risk 4: JSON column performance**
- JSONB queries are slower than indexed columns.
- → Mitigation: `real_sources` is small (typically 1-3 items). Queries are infrequent (only during fetch).

**Risk 5: Real source name collisions**
- Different libraries might use the same real source name (e.g., both akshare and fd-world use "eastmoney").
- → Mitigation: Real source names are global (not scoped to library). Document canonical names (e.g., "eastmoney", "tencent", "sina", "yahoo_finance").

## Migration Plan

1. **Schema change**: Add `RealSourceSpec` to `fd-open-data-protocol/schema.py`. Add `real_sources` field to `FunctionSpec`.
2. **Database migration**: Add `real_sources` JSONB column to `functions` table.
3. **Update models**: Add `real_sources` field to `Function` model in `fd-open-data-mcp/models.py`.
4. **Update register**: Update `register_datasource()` to persist `real_sources` from manifest to database.
5. **Update circuit breaker**: Change keys from `circuit:{source}:{proxy_id}` to `circuit:{real_source}:{proxy_id}`.
6. **Update failover**: Implement priority-based failover in `fetch/dispatch.py`.
7. **Update proxy validation**: Test proxies against real data sources (if declared).
8. **Update manifests**: Add `real_sources` to existing manifests (optional, incremental).
9. **Deploy**: Roll out to staging, monitor, then production.
10. **Rollback**: If issues, revert schema change and database migration. Existing manifests without `real_sources` continue to work.

## Open Questions

1. **Real source name registry**: Should we maintain a canonical list of real source names (e.g., "eastmoney", "tencent", "sina")? Or allow arbitrary names?
2. **Endpoint field**: Is `endpoint` field useful? Or should we remove it (simplify schema)?
3. **Auto-discovery**: Should we add auto-discovery of real sources from function names (e.g., `_em` → eastmoney)? Or keep it manual?
4. **Monitoring**: How should we monitor real source health? Separate dashboard? Alerts?

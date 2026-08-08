## 1. Schema changes in fd-open-data-protocol

- [x] 1.1 Add `RealSourceSpec` Pydantic model to `fd-open-data-protocol/schema.py` (fields: name, priority, endpoint)
- [x] 1.2 Add optional `real_sources` field to `FunctionSpec` in `fd-open-data-protocol/schema.py`
- [x] 1.3 Update example manifest in `examples/example_stock.yaml` with `real_sources` declaration
- [x] 1.4 Verify schema validation works (pass YAML manifest with real_sources through validator)
- [x] 1.5 Write unit tests for RealSourceSpec validation (valid and invalid inputs)

## 2. Database migration

- [x] 2.1 Create SQL migration script to add `real_sources` JSONB column to `functions` table
- [x] 2.2 Run migration on local Postgres instance (test environment)
- [x] 2.3 Verify existing functions still work (real_sources is NULL by default)
- [x] 2.4 Test upsert of functions with real_sources via SQLAlchemy
- [x] 2.5 Document migration rollback plan (DROP COLUMN if issues)

## 3. Model updates in fd-open-data-mcp

- [x] 3.1 Add `real_sources` field to `Function` model in `fd-open-data-mcp/models.py`
- [x] 3.2 Update function loading logic to parse JSON real_sources (list[dict])
- [x] 3.3 Add helper method `get_primary_real_source()` on Function model
- [x] 3.4 Write unit tests for Function.real_sources parsing

## 4. Catalog register updates

- [x] 4.1 Update `register_datasource()` in `catalog/register.py` to persist real_sources from manifest
- [x] 4.2 Handle real_sources conversion (Pydantic model -> dict -> JSONB)
- [x] 4.3 Write integration test: register a manifest with real_sources, verify DB persistence
- [x] 4.4 Write backward compatibility test: register old manifest without real_sources, verify NULL persists

## 5. Proxy pool circuit breaker updates

- [x] 5.1 Update `circuit.py` to accept `real_source` parameter instead of `source`
- [x] 5.2 Update circuit key generation: `circuit:{real_source}:{proxy_id}` vs `circuit:{source}:{proxy_id}`
- [x] 5.3 Add fallback logic: when real_source is None, fall back to source (library-level)
- [x] 5.4 Circuit supports both real_source and library_name via same key format
- [x] 5.5 Write unit tests for circuit state per real_source (14 tests passed)

## 6. Fetch instrumentation updates

- [x] 6.1 Update `instrumented_fetch()` in `fetch/instrumentation.py` to accept real_source parameter
- [x] 6.2 Update `_record()` to write real_source to fetch_log table
- [x] 6.3 Add real_source column to FetchLog model in models.py
- [x] 6.4 Create migration script to add real_source column to fetch_log table
- [x] 6.5 Apply migration to database successfully

## 7. Failover logic updates

- [x] 7.1 Update `dispatch.py` to implement real_source-based failover
- [x] 7.2 When primary real_source is banned, try next priority real_source
- [x] 7.3 If all real_sources are banned, raise `SourceUnavailable`
- [x] 7.4 Write integration test: ban eastmoney, verify auto-failover to tencent
- [x] 7.5 Log failover events (INFO level): "real_source failover: {real_source} -> trying next priority"

## 8. Proxy pool sync updates

- [x] 8.1 Update `proxy_pool_sync.py` to validate proxies against real_data_sources (eastmoney, tencent, yahoo_finance)
- [x] 8.2 For each proxy, test against each real_source's endpoint
- [x] 8.3 Backward compatibility: proxies validated against any real_source are accepted
- [x] 8.4 Update logging: show validation success rate per real_source
- [x] 8.5 Integration test: proxy sync validates against eastmoney/tencent/yahoo_finance

## 9. Documentation and examples

- [x] 9.1 Update CLAUDE.md with real_sources concept explanation
- [x] 9.2 Create example manifest showing real_sources usage (updated example_stock.yaml)
- [x] 9.3 Document canonical real source names (eastmoney, tencent, sina, yahoo_finance)
- [x] 9.4 Add troubleshooting guide: how to check real_source health (in CLAUDE.md)

## 10. Testing and deployment

- [ ] 10.1 Run E2E tests on staging environment
- [ ] 10.2 Monitor proxy pool sync for 1 week (verify validation success rates)
- [ ] 10.3 Monitor failover events (should be rare unless sources are actually banned)
- [ ] 10.4 Deploy to production
- [ ] 10.5 Monitor post-deployment: check for errors, alert if issues arise

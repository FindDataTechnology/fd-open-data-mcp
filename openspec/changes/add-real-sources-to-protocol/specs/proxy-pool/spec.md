## MODIFIED Requirements

### Requirement: Proxy pool sync CronJob
A k8s CronJob SHALL periodically (every 10 minutes) synchronize proxies from the proxy pool service to the `proxies` table in PostgreSQL. The CronJob SHALL validate each proxy against real data sources (if declared in function manifests), not library-level sources. When a function declares `real_sources` (e.g., eastmoney, tencent), the CronJob SHALL validate the proxy against each real source's endpoint. When a function does not declare `real_sources`, the CronJob SHALL fall back to library-level validation (backward compatibility).

#### Scenario: Sync CronJob runs every 10 minutes
- **WHEN** the CronJob schedule triggers (every 10 minutes)
- **THEN** the CronJob fetches proxies from the proxy pool API (`GET /get?count=50`)
- **AND** validates each proxy against real data sources (if declared in manifests)
- **AND** upserts working proxies to the `proxies` table (scheme, ip, port, status=active)
- **AND** marks proxies not seen in 3 sync cycles as `retired`

#### Scenario: Proxy validation against real data sources
- **WHEN** the sync CronJob validates a proxy
- **AND** a function declares `real_sources: [{name: eastmoney}, {name: tencent}]`
- **THEN** it makes an HTTP request through the proxy to eastmoney's endpoint
- **AND** makes an HTTP request through the proxy to tencent's endpoint
- **AND** if the proxy successfully connects to at least one real source (HTTP 2xx or 5xx), it's considered valid
- **AND** if the proxy fails to connect to all real sources (timeout, connection refused), it's considered invalid
- **AND** only valid proxies are upserted to the `proxies` table

#### Scenario: Proxy validation fallback to library-level
- **WHEN** the sync CronJob validates a proxy
- **AND** no function declares `real_sources`
- **THEN** it falls back to library-level validation (e.g., akshare functions)
- **AND** if the proxy successfully connects, it's considered valid

#### Scenario: Proxy upsert to proxies table
- **WHEN** the sync CronJob validates a proxy successfully
- **THEN** it upserts the proxy to the `proxies` table
- **AND** sets `status = 'active'`
- **AND** sets `scheme`, `ip`, `port` from the proxy pool response
- **AND** if the proxy already exists (same ip+port), it updates the existing row

#### Scenario: Stale proxy retirement
- **WHEN** a proxy in the `proxies` table has not been seen in the last 3 sync cycles (30 minutes)
- **THEN** the sync CronJob marks the proxy as `retired`
- **AND** sets `status = 'retired'`
- **AND** sets `retired_at = NOW()`
- **AND** the proxy is no longer selected by `ProxySelector`

### Requirement: Circuit breaker tracks per-real-source health
The circuit breaker SHALL track health per `(real_source, proxy_id)`, not per `(source, proxy_id)`. When a function declares `real_sources`, the circuit breaker key SHALL be `circuit:{real_source}:{proxy_id}` (e.g., `circuit:eastmoney:2`). When a function does not declare `real_sources`, the circuit breaker SHALL fall back to `circuit:{source}:{proxy_id}` (backward compatibility).

#### Scenario: Circuit breaker key for function with real_sources
- **WHEN** a function declares `real_sources: [{name: eastmoney, priority: 0}]`
- **AND** a fetch through proxy 2 fails with a ban-classified error
- **THEN** the circuit breaker key SHALL be `circuit:eastmoney:2`
- **AND** the fail streak for `(eastmoney, proxy 2)` is incremented

#### Scenario: Circuit breaker key for function without real_sources
- **WHEN** a function does not declare `real_sources`
- **AND** a fetch through proxy 2 fails with a ban-classified error
- **THEN** the circuit breaker key SHALL be `circuit:akshare:2` (library-level fallback)
- **AND** the fail streak for `(akshare, proxy 2)` is incremented

#### Scenario: Different real sources have independent circuits
- **WHEN** `circuit:eastmoney:2` is OPEN (banned)
- **AND** `circuit:tencent:2` is CLOSED (healthy)
- **THEN** a function with `real_sources: [{name: eastmoney}, {name: tencent}]` SHALL failover to tencent
- **AND** the tencent circuit state is unchanged

## ADDED Requirements

### Requirement: Real source name registry
The system SHALL maintain a canonical list of real source names (e.g., "eastmoney", "tencent", "sina", "yahoo_finance"). The list SHALL be documented in the `fd-open-data-protocol` package. Manifest authors SHALL use names from this list when declaring `real_sources`.

#### Scenario: Canonical real source names
- **WHEN** a manifest declares `real_sources: [{name: eastmoney}]`
- **THEN** "eastmoney" SHALL be in the canonical list
- **AND** the manifest SHALL validate successfully

#### Scenario: Non-canonical real source name
- **WHEN** a manifest declares `real_sources: [{name: unknown_source}]`
- **THEN** the manifest SHALL still validate (no strict validation)
- **AND** a warning SHALL be logged (non-canonical name)

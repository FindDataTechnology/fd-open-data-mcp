"""Shared fetch instrumentation - the single chokepoint both ``concept-fetch``
dispatch and the scraw ``fetch_handler`` route through.

In the standalone-proxy-service design (openspec change ``add-proxy-service``),
per-fetch proxy selection lives in the forwarder (``proxy-fw`` pod), NOT here.
Per fetch:
  1. ``proxy_client.acquire(source)`` asks the forwarder for an upstream URL ->
     an ``Acquisition``. A direct sentinel (``upstream_url=None, addr_id=None``)
     is returned when the forwarder is unset (ships-dark / local dev), OR when
     it has no healthy upstream (all circuits OPEN).
  2. ``use_proxy(acq.upstream_url)`` injects the URL into requests/httpx (None =
     direct egress, identical to today's ``scheme='direct'``). The crawler
     terminates TLS itself, so only *it* sees the decrypted response needed to
     classify a ban — this is why the contract is acquire/release, not a blind
     ``HTTP_PROXY`` env-set (see ``injection.py`` module docstring).
  3. ``run_upstream`` is called (timed).
  4. ``ban_rules.classify`` maps the outcome to ok/transient/ban/blocked.
     ``fail_streak`` is read from the local ``circuit`` view (``REDIS_URL``) for
     streak-gated rules; when REDIS_URL points at proxy-redis this is the live
     streak the forwarder owns, ships-dark (no redis) -> 0 (streak rules inert,
     same as today).
  5. ``proxy_client.release(...)`` hands the classification back to the
     forwarder, which owns the circuit state machine (writes to proxy-redis).
     ``_record`` writes ``fetch_log`` (cold) + the outcomes stream (hot).

TRANSIENT => retry once on the same upstream. BAN => release + re-acquire
(the forwarder picks a different upstream — the banned one's circuit is now
OPEN so ``acquire_any`` skips it). Upstream loop exhausted, OR a direct-sentinel
fetch banned => ``SourceUnavailable`` (caller fails over to the next
real_source). ``blocked`` => raise ``FetchError`` (no point burning another
upstream). Behavior above the transport layer is unchanged.

Degrades to today's behavior when ``FD_PROXY_FORWARDER`` is unset: ``acquire``
returns the direct sentinel, ``release`` is a no-op, fetches egress direct from
the worker's own node IP — no rotation, but still functional.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.fetch.runner import FetchError, run_upstream
from fd_open_data_mcp.models import FetchLog
from fd_open_data_mcp.proxy import ban_rules, circuit, injection

logger = logging.getLogger(__name__)


# Sources that bypass the proxy/circuit pipeline and run direct. These are
# authenticated REST APIs (key/header auth) that manage their own HTTP transport
# and gain nothing from upstream rotation — injecting dead proxies only breaks
# them. ``cn-report`` uses akshare->eastmoney internally (IP-scraped, but the
# inner akshare call rejects proxies); ``polygon`` is key-authenticated
# (``POLYGON_API_KEY`` header). Both still get a timed ``run_upstream`` +
# ``fetch_log`` entry; they just skip the acquire/release/retry loop.
_DIRECT_SOURCES = frozenset({"cn-report", "polygon", "datacommons"})


class SourceUnavailable(Exception):
    """Every proxy for a source is OPEN/saturated - caller should fail over to
    the next source in the plan's ranked_sources chain."""


def _is_readonly_tx(exc: BaseException) -> bool:
    """True when ``exc`` is a write against a read-only Postgres transaction.

    Postgres raises ``cannot execute INSERT in a read-only transaction``
    (SQLSTATE 25006, ``ReadOnlySqlTransaction``) when a DB is in read-only mode
    — e.g. the retired America / LAN replicas kept around for rollback. A bare
    ``except`` previously swallowed these at ``debug``, silently dropping every
    ``fetch_log`` row; detected here so the caller logs at WARNING with a clear
    message instead (Bug 6). Best-effort: checks the unwrapped DBAPI exception
    class name, SQLSTATE/pgcode, and message text, so it works across
    psycopg2/psycopg3 without a hard import dependency.
    """
    orig = getattr(exc, "orig", exc)  # unwrap SQLAlchemy OperationalError
    if type(orig).__name__ == "ReadOnlySqlTransaction":
        return True
    if getattr(orig, "pgcode", None) == "25006":  # sql_read_only_transaction
        return True
    msg = str(orig).lower()
    return "read-only transaction" in msg


def _record(session, source: str, proxy_id: Optional[int], classification: str,
            latency_ms: int, status: str, detail: Optional[str],
            concept_id: Optional[int] = None, entity_type: Optional[str] = None,
            entity_id: Optional[int] = None, real_source: Optional[str] = None) -> None:
    """Write fetch_log (cold) + the outcomes stream (hot).

    Args:
        source: Library-level source name (e.g., "akshare")
        real_source: Real data source name (e.g., "eastmoney") if available
    """
    try:
        session.add(FetchLog(
            source=source, concept_id=concept_id, entity_type=entity_type,
            entity_id=entity_id, latency_ms=latency_ms, status=status,
            detail=detail[:500] if detail else None,
            proxy_id=proxy_id, classification=classification,
            real_source=real_source,
        ))
        session.commit()
    except Exception as e:  # noqa: BLE001 - never let logging break the fetch
        session.rollback()
        if _is_readonly_tx(e):
            # Writing to a read-only Postgres (a retired rollback-only
            # replica, e.g. the old America / LAN DBs). fetch_log can't
            # persist there — surface it loudly so ops don't silently lose
            # every log row (Bug 6). Still non-breaking: never raises.
            logger.warning(
                "fetch_log write failed: database is read-only (source=%s "
                "proxy=%s) — likely a retired rollback-only replica; %s",
                source, proxy_id, e)
        else:
            logger.warning("fetch_log write failed (source=%s): %s", source, e)
    # Use real_source for outcomes stream if available, otherwise fall back to source
    outcome_source = real_source if real_source else source
    circuit.write_outcome(outcome_source, {
        "source": outcome_source, "proxy_id": proxy_id or 0,
        "classification": classification, "status": status,
        "latency_ms": latency_ms, "at": datetime.now(timezone.utc).isoformat(),
    })


def instrumented_fetch(
    source: str,
    command: str,
    params: dict,
    *,
    session=None,
    concept_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    max_proxies: int = 3,
    real_source: Optional[str] = None,
) -> Any:
    """Run an upstream fetch through the proxy/circuit pipeline. Returns the
    upstream result. Raises ``FetchError`` on a transient failure that exhausts
    retries, or ``SourceUnavailable`` when every proxy for the source is down.

    If ``session`` is omitted, a short-lived session is created (for the scraw
    fetch_handler path); callers with their own session (dispatch) should pass
    it so fetch_log + proxy queries share their transaction.

    Args:
        source: Library-level source name (e.g., "akshare")
        real_source: Real data source name (e.g., "eastmoney") if available.
                     Used for circuit breaker tracking and ban classification.
    """
    injection.install()
    own_session = session is None
    if own_session:
        session = get_database().get_session()
    tried: list[int] = []
    last_error: Optional[str] = None
    # Use real_source for circuit operations if available, otherwise fall back to source
    circuit_source = real_source if real_source else source

    # Authenticated REST APIs that manage their own HTTP transport and gain nothing
    # from proxy rotation (they are not IP-scraped). cn-report uses akshare->eastmoney
    # internally, which breaks under proxy injection; polygon is key-authenticated
    # (POLYGON_API_KEY header) — proxying a free-proxy pool only breaks it. Both run
    # direct: no ProxySelector, no circuit, just a timed run_upstream + fetch_log.
    if source in _DIRECT_SOURCES:
        t0 = time.time()
        try:
            value = run_upstream(source, command, params)
            elapsed_ms = int((time.time() - t0) * 1000)
            _record(session, source, None, "ok", elapsed_ms, "ok", None,
                    concept_id, entity_type, entity_id, real_source)
            return value
        except FetchError as e:
            elapsed_ms = int((time.time() - t0) * 1000)
            _record(session, source, None, "error", elapsed_ms, "error", str(e),
                    concept_id, entity_type, entity_id, real_source)
            raise

    try:
        for _ in range(max_proxies):
            # Ask the forwarder for an upstream. Returns a direct sentinel
            # (upstream_url=None, addr_id=None) when the forwarder is unset
            # (ships-dark / local dev) OR when it has no healthy upstream (all
            # circuits OPEN). use_proxy(None) => direct egress, so the sentinel
            # path degrades to a plain direct fetch with no rotation.
            # Pass tried addr_ids so the forwarder skips a proxy that already
            # failed within THIS fetch's retry loop (Bug 5). Without it a dead
            # proxy is re-acquired on the next max_proxies iteration; with it the
            # forwarder rotates to a different upstream. Empty on the first pass.
            acq = injection.proxy_client.acquire(circuit_source, exclude=tried)
            tried.append(acq.addr_id or 0)
            for attempt in range(2):  # 1 retry on transient
                t0 = time.time()
                classification = "ok"
                status = "ok"
                detail: Optional[str] = None
                value: Any = None
                try:
                    with injection.use_proxy(acq.upstream_url):
                        value = run_upstream(source, command, params)
                except FetchError as e:
                    detail = str(e)
                    last_error = detail
                    status = "error"
                    st = circuit.get_state(circuit_source, acq.addr_id) if acq.addr_id is not None else {"fail_streak": 0}
                    # Thread the HTTP status/body carried by FetchError so
                    # status-based + body-based ban rules (403/429/captcha) can
                    # match. Connection errors (RemoteDisconnected, timeout)
                    # carry None for both -> classify defaults to transient.
                    # Combined streak = max(fail, transient) so a streak_min gate
                    # on a transient-class rule can fire (read-only: this value
                    # never touches fail_streak in the hash).
                    combined_streak = max(st["fail_streak"], st.get("transient_streak", 0))
                    classification = ban_rules.classify(
                        session, circuit_source, e.status_code, detail,
                        e.response_text, combined_streak
                    )
                elapsed_ms = int((time.time() - t0) * 1000)
                # Hand the classification to the forwarder, which owns the
                # circuit state machine (writes to proxy-redis via /release).
                # No-op in ships-dark (no forwarder) or when addr_id is None
                # (direct sentinel — no circuit to update).
                injection.proxy_client.release(
                    circuit_source, acq.addr_id, acq.provider, classification)
                _record(session, source, acq.addr_id, classification, elapsed_ms,
                        status, detail, concept_id, entity_type, entity_id, real_source)
                if classification == "ok":
                    return value
                if classification == "blocked":
                    # needs human/strategy change - don't burn another upstream
                    raise FetchError(f"{source}/{command} blocked: {detail}")
                if classification == "transient" and attempt == 0:
                    continue  # retry same upstream once
                break  # ban -> re-acquire (forwarder skips the now-OPEN circuit)
        raise SourceUnavailable(
            f"no healthy proxy for {circuit_source} (tried {tried}; last_error={last_error})"
        )
    finally:
        if own_session:
            session.close()

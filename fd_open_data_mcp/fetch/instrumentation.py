"""Shared fetch instrumentation - the single chokepoint both ``concept-fetch``
dispatch and the scraw ``fetch_handler`` route through.

Per fetch:
  1. ``ProxySelector`` picks a healthy ``(source, proxy_id)`` (CLOSED + under
     rate limit), direct first. ``None`` => every proxy banned/saturated =>
     ``SourceUnavailable`` (caller fails over to the next source).
  2. ``use_proxy`` sets the contextvar so the requests/httpx patch injects it.
  3. ``run_upstream`` is called (timed).
  4. ``BanClassifier`` classifies the outcome (error-message based in v1 - the
     parsed DataFrame loses the raw HTTP body; body/status rules need an
     adapter-level hook, tracked as a follow-up).
  5. ``CircuitUpdater`` records the outcome (Redis circuit + ``fetch_log`` with
     ``proxy_id`` + ``classification``) and appends to the ``outcomes:{source}``
     stream.

TRANSIENT => retry once on the same proxy. BAN => circuit trips, re-select
another proxy for the same source. All proxies exhausted => SourceUnavailable.

Degrades to today's behavior when REDIS_URL is unset (no proxies registered =>
direct, no circuit, no rate limit).
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
from fd_open_data_mcp.proxy.selector import ProxySelector

logger = logging.getLogger(__name__)


class SourceUnavailable(Exception):
    """Every proxy for a source is OPEN/saturated - caller should fail over to
    the next source in the plan's ranked_sources chain."""


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
        logger.debug("fetch_log write failed: %s", e)
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

    # cn-report adapter uses akshare internally (for financial values), which breaks
    # under proxy injection (akshare calls eastmoney directly). Skip proxy for cn-report.
    if source == "cn-report":
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
        selector = ProxySelector(session)
        for _ in range(max_proxies):
            picked = selector.select(circuit_source)
            if picked is None:
                break  # no healthy proxy -> source-level failover
            proxy_id, proxy = picked
            tried.append(proxy_id if proxy_id is not None else 0)
            for attempt in range(2):  # 1 retry on transient
                t0 = time.time()
                classification = "ok"
                status = "ok"
                detail: Optional[str] = None
                value: Any = None
                try:
                    with injection.use_proxy(proxy):
                        value = run_upstream(source, command, params)
                except FetchError as e:
                    detail = str(e)
                    last_error = detail
                    status = "error"
                    st = circuit.get_state(circuit_source, proxy_id) if proxy_id is not None else {"fail_streak": 0}
                    classification = ban_rules.classify(
                        session, circuit_source, None, detail, None, st["fail_streak"]
                    )
                elapsed_ms = int((time.time() - t0) * 1000)
                if proxy_id is not None:
                    circuit.record_outcome(circuit_source, proxy_id, classification)
                _record(session, source, proxy_id, classification, elapsed_ms,
                        status, detail, concept_id, entity_type, entity_id, real_source)
                if classification == "ok":
                    return value
                if classification == "blocked":
                    # needs human/strategy change - don't burn another proxy
                    raise FetchError(f"{source}/{command} blocked: {detail}")
                if classification == "transient" and attempt == 0:
                    continue  # retry same proxy once
                break  # ban -> try next proxy (circuit now OPEN, selector skips it)
        raise SourceUnavailable(
            f"no healthy proxy for {circuit_source} (tried {tried}; last_error={last_error})"
        )
    finally:
        if own_session:
            session.close()

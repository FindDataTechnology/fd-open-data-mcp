"""Stream health observability (task 3.6).

The stream-plane「消费滞后与积压可见」contract: backlog, oldest unacked age,
consumer status, and an alert signal when the backlog crosses a threshold.
The panel's stream board (analysis-plane, P4) reads this; it is read-only and
touches no crawl state.

A threshold alert is a boolean on the summary (not a side-effecting page) —
the panel renders it, and a future alerting hook can subscribe to the same
signal. The threshold is per-call so different surfaces (ops board vs. a
tight SLA check) can judge the same numbers by different bars.
"""
from __future__ import annotations

from fd_open_data_mcp.stream.sink import EventStream, PendingSummary

DEFAULT_BACKLOG_ALERT = 10_000
DEFAULT_LAG_ALERT_MS = 30 * 60 * 1000  # 30 min of unacked age


def stream_health(stream: EventStream, group: str, *,
                  backlog_alert: int = DEFAULT_BACKLOG_ALERT,
                  lag_alert_ms: int = DEFAULT_LAG_ALERT_MS) -> dict:
    """Health snapshot for one consumer group.

    Returns ``{length, pending, oldest_pending_idle_ms, consumers, alert,
    alert_reasons}``. ``alert`` is True when the backlog exceeds
    ``backlog_alert`` or the oldest unacked message is older than
    ``lag_alert_ms`` (or the group has no live consumer while messages are
    pending — a stalled pipeline).
    """
    s: PendingSummary = stream.pending_summary(group)
    reasons: list[str] = []
    if s.pending > backlog_alert:
        reasons.append(f"backlog {s.pending} > {backlog_alert}")
    if s.oldest_pending_idle_ms is not None and s.oldest_pending_idle_ms > lag_alert_ms:
        reasons.append(f"lag {s.oldest_pending_idle_ms}ms > {lag_alert_ms}ms")
    if s.pending > 0 and s.consumers == 0:
        reasons.append("pending with no live consumer")
    return {
        "length": s.length,
        "pending": s.pending,
        "oldest_pending_idle_ms": s.oldest_pending_idle_ms,
        "consumers": s.consumers,
        "alert": bool(reasons),
        "alert_reasons": reasons,
    }

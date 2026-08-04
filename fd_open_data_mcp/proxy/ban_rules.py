"""Ban-classification rule engine.

Maps a fetch outcome ``(http_status, error, body)`` to one of
``ok / transient / ban / blocked`` using a per-source rule set from the
``ban_rules`` table. Rules are matched in priority order (desc); the first match
wins. ``streak_min`` gates a rule (e.g. ``RemoteDisconnected -> ban`` only after
the fail streak is already >= 3, so a single network blip is transient).

Default (no rule matches): 2xx -> ok, else transient. Rules are data - a new
source declares its ban signals at registration, no code change.

Rule types:
  status : pattern matches the HTTP status (``"403"``, ``"429"``, ``"5xx"``)
  error  : pattern is a substring of the exception message
  body   : pattern is a regex tested against the response body
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import BanRule

logger = logging.getLogger(__name__)

# In-memory rule cache: source -> (loaded_at, [dict]). TTL 60s so rule edits
# propagate without a restart. We cache plain dicts to avoid ORM detached instance
# errors when the DB session closes after use.
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL = 60.0


def _load_rules(session: Session, source: str) -> list[dict]:
    now = time.time()
    cached = _CACHE.get(source)
    if cached and now - cached[0] < _TTL:
        return cached[1]
    ban_rules = (
        session.query(BanRule)
        .filter(BanRule.source == source, BanRule.enabled.is_(True))
        .order_by(BanRule.priority.desc())
        .all()
    )
    # Convert ORM objects to plain dicts to avoid detached instance errors
    rules_dicts = [
        {
            "streak_min": br.streak_min,
            "rule_type": br.rule_type,
            "pattern": br.pattern,
            "classification": br.classification,
        }
        for br in ban_rules
    ]
    _CACHE[source] = (now, rules_dicts)
    return rules_dicts


def _status_matches(pattern: str, http_status: Optional[int]) -> bool:
    if http_status is None:
        return False
    p = pattern.strip().lower()
    if p.endswith("xx"):
        try:
            return http_status // 100 == int(p[0])
        except ValueError:
            return False
    try:
        return http_status == int(p)
    except ValueError:
        return False


def _error_matches(pattern: str, error: Optional[str]) -> bool:
    if not error:
        return False
    return pattern.lower() in error.lower()


def _body_matches(pattern: str, body: Optional[str]) -> bool:
    if not body:
        return False
    try:
        return re.search(pattern, body) is not None
    except re.error:
        return pattern in body


def classify(
    session: Session,
    source: str,
    http_status: Optional[int],
    error: Optional[str],
    body: Optional[str],
    fail_streak: int = 0,
) -> str:
    """Classify an outcome. Returns ok / transient / ban / blocked."""
    rules = _load_rules(session, source)
    for rule in rules:
        if rule["streak_min"] and fail_streak < rule["streak_min"]:
            continue
        matched = (
            (rule["rule_type"] == "status" and _status_matches(rule["pattern"], http_status))
            or (rule["rule_type"] == "error" and _error_matches(rule["pattern"], error))
            or (rule["rule_type"] == "body" and _body_matches(rule["pattern"], body))
        )
        if matched:
            return rule["classification"]
    # default
    if http_status is not None and 200 <= http_status < 300:
        return "ok"
    return "transient"

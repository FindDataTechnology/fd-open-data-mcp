"""Retrieval scopes (indicator-scope spec): model, CRUD, enforcement, stats.

A scope is a named allow-list object stored in ``fd_open_data.scopes`` —
``rules = {source_dbs: [], domains: [], semantic_codes: [], native_codes: []}``
(design D3). Enforcement lives here, in the shared library, so both
fd-open-data-mcp's search/read tools and business-mcp's single-source domain
search tools consume the same semantics (design D4):

  - a row passes when EVERY non-empty allow-list admits it; an empty list
    means "no constraint on this dimension";
  - local concept rows carry no registry ``source_db``/``native_code``, so a
    scope that pins those dimensions excludes local rows — intended: a
    yearbook-only scope must not leak world_bank or local concepts;
  - ``unscoped`` is the reserved escape name (explicit request to run over
    the full corpus — never creatable, design D5);
  - a caller may bind a default scope (``scope_bindings``, opaque
    ``caller_key``); an explicit parameter always wins over the default.

Scope is a precision/cost tool, NOT a permission wall (design non-goal):
any caller may reference any scope. Every scoped response carries the
scope's name and definition summary so scoped emptiness is distinguishable
from true absence (spec「Scoped responses disclose the active scope」), and
every scoped retrieval increments the per-(scope, day) hit counters.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Any, Optional

from sqlalchemy import bindparam, func, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Concept, Scope, ScopeBinding, ScopeStat

logger = logging.getLogger(__name__)

UNSCOPED = "unscoped"
RESERVED_NAMES = {UNSCOPED}
RULE_DIMENSIONS = ("source_dbs", "domains", "semantic_codes", "native_codes")
CALLER_KEY_ENV = "FD_MCP_CALLER_KEY"
CALLER_HEADER = "x-fd-caller"


class ScopeNotFound(LookupError):
    """Referenced scope does not exist — tools surface it with the name."""

    def __init__(self, name: str, context: str = ""):
        self.name = name
        message = f"unknown scope: {name!r}"
        if context:
            message += f" ({context})"
        super().__init__(message)


# ─── Rules normalization + live-table validation (design D7) ────────────────

def normalize_rules(rules: Any) -> dict[str, list[str]]:
    """Coerce a rules payload to ``{dimension: [str, ...]}``; reject junk.

    At least one non-empty allow-list is required — a scope with no
    allow-lists constrains nothing and is not a scope.
    """
    if not isinstance(rules, dict):
        raise ValueError("rules must be an object with allow-list keys: "
                         + ", ".join(RULE_DIMENSIONS))
    out: dict[str, list[str]] = {}
    for dim in RULE_DIMENSIONS:
        raw = rules.get(dim) or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            raise ValueError(f"rules.{dim} must be a list of strings")
        values = [v.strip() for v in raw if v.strip()]
        out[dim] = sorted(dict.fromkeys(values))  # dedupe, keep deterministic order
    if not any(out.values()):
        raise ValueError(
            "rules carry no allow-lists (all of " + ", ".join(RULE_DIMENSIONS)
            + " empty) — such a scope would constrain nothing"
        )
    return out


def validate_rules(session: Session, rules: dict[str, list[str]]) -> tuple[int, list[str], dict]:
    """Count live matches per dimension against registry_entries + concepts.

    Returns ``(total_matches, warnings, per_dimension)``. Unknown codes are a
    warning, not a rejection (native_code drift, design trade-off) — but a
    scope whose allow-lists match NOTHING known is rejected by the caller
    (spec「Empty scopes are rejected」). Verified registry rows and
    non-deprecated concepts are the live surfaces — what retrieval can
    actually serve.
    """
    per_dim: dict[str, int] = {}
    warnings: list[str] = []
    total = 0

    def _registry_count(column: str, values: list[str]) -> int:
        try:
            stmt = text(
                f"SELECT COUNT(*) FROM registry_entries "
                f"WHERE verified AND {column} IN :vals"
            ).bindparams(bindparam("vals", expanding=True))
            return session.execute(stmt, {"vals": values}).scalar() or 0
        except (OperationalError, ProgrammingError):
            return 0  # registry absent (dev SQLite): registry dimensions match 0

    for dim in RULE_DIMENSIONS:
        values = rules.get(dim) or []
        if not values:
            per_dim[dim] = 0
            continue
        if dim == "semantic_codes":
            reg = _registry_count("semantic_code", values)
            nat = (
                session.query(func.count(func.distinct(Concept.code)))
                .filter(Concept.deprecated.is_(False), Concept.code.in_(values))
                .scalar() or 0
            )
            per_dim[dim] = reg + nat
        else:
            column = {"source_dbs": "source_db", "domains": "domain",
                      "native_codes": "native_code"}[dim]
            per_dim[dim] = _registry_count(column, values)
        unknown = [v for v in values if v not in _known_values(session, dim)]
        if unknown:
            warnings.append(
                f"rules.{dim} values not found in any live table: {', '.join(unknown)}"
            )
        total += per_dim[dim]
    return total, warnings, per_dim


def _known_values(session: Session, dim: str) -> set[str]:
    """Live known values for a dimension (for unknown-code warnings)."""
    try:
        if dim == "semantic_codes":
            reg = {r[0] for r in session.execute(
                text("SELECT DISTINCT semantic_code FROM registry_entries WHERE verified"))}
            nat = {r[0] for r in session.execute(
                text("SELECT DISTINCT code FROM concepts WHERE deprecated = 0"))}
            return reg | nat
        column = {"source_dbs": "source_db", "domains": "domain",
                  "native_codes": "native_code"}[dim]
        return {r[0] for r in session.execute(
            text(f"SELECT DISTINCT {column} FROM registry_entries WHERE verified"))}
    except (OperationalError, ProgrammingError):
        return set()


# ─── CRUD ────────────────────────────────────────────────────────────────────

def scope_create(session: Session, name: str, rules: Any,
                 description: Optional[str] = None) -> dict:
    """Create a scope; rejects empty-matching rules and reserved names."""
    name = (name or "").strip()
    if not name:
        raise ValueError("scope name is required")
    if name.lower() in RESERVED_NAMES:
        raise ValueError(f"{name!r} is a reserved scope name (explicit-unscoped escape)")
    if session.query(Scope).filter_by(name=name).first() is not None:
        raise ValueError(f"scope {name!r} already exists")
    norm = normalize_rules(rules)
    matched, warnings, per_dim = validate_rules(session, norm)
    if matched == 0:
        raise ValueError(
            "scope matches nothing known (empty scope): every allow-list value "
            f"is absent from registry_entries/concepts; per-dimension matches: "
            f"{per_dim}"
        )
    row = Scope(name=name, description=description, rules=norm)
    session.add(row)
    session.commit()
    out = row.toDict()
    out["matched"] = matched
    out["per_dimension_matches"] = per_dim
    if warnings:
        out["warnings"] = warnings
    return out


def scope_update(session: Session, name: str, rules: Any,
                 description: Optional[str] = None) -> dict:
    """Update rules and/or description; unknown scope is an explicit error."""
    row = session.query(Scope).filter_by(name=name).first()
    if row is None:
        raise ScopeNotFound(name, "cannot update")
    matched = warnings = None
    per_dim = None
    if rules is not None:
        norm = normalize_rules(rules)
        matched, warnings, per_dim = validate_rules(session, norm)
        if matched == 0:
            raise ValueError(
                "scope update matches nothing known (empty scope): "
                f"per-dimension matches: {per_dim}"
            )
        row.rules = norm
    if description is not None:
        row.description = description
    session.commit()
    out = row.toDict()
    if matched is not None:
        out["matched"] = matched
        out["per_dimension_matches"] = per_dim
        if warnings:
            out["warnings"] = warnings
    return out


def scope_delete(session: Session, name: str) -> dict:
    """Delete a scope and its caller default bindings (stats are history)."""
    row = session.query(Scope).filter_by(name=name).first()
    if row is None:
        raise ScopeNotFound(name, "cannot delete")
    session.query(ScopeBinding).filter_by(scope_name=name).delete()
    session.delete(row)
    session.commit()
    return {"deleted": name, "bindings_removed": True}


def scope_list(session: Session) -> list[dict]:
    """Every scope with its rules (management surface, design D3)."""
    return [s.toDict() for s in session.query(Scope).order_by(Scope.name).all()]


def bind_caller(session: Session, caller_key: str, scope_name: str) -> dict:
    """Bind a caller's default scope (applies when no explicit scope passed)."""
    caller_key = (caller_key or "").strip()
    if not caller_key:
        raise ValueError("caller_key is required")
    if scope_name.lower() in RESERVED_NAMES:
        raise ValueError(f"{scope_name!r} is reserved; unbind instead")
    if session.query(Scope).filter_by(name=scope_name).first() is None:
        raise ScopeNotFound(scope_name, "cannot bind")
    binding = session.get(ScopeBinding, caller_key)
    if binding is None:
        binding = ScopeBinding(caller_key=caller_key, scope_name=scope_name)
        session.add(binding)
    else:
        binding.scope_name = scope_name
    session.commit()
    return binding.toDict()


def unbind_caller(session: Session, caller_key: str) -> dict:
    """Drop a caller's default binding; unknown binding is explicit."""
    binding = session.get(ScopeBinding, (caller_key or "").strip())
    if binding is None:
        raise ValueError(f"no scope binding for caller {caller_key!r}")
    out = binding.toDict()
    session.delete(binding)
    session.commit()
    return {"unbound": out["caller_key"], "scope_name": out["scope_name"]}


def list_bindings(session: Session, caller_key: Optional[str] = None) -> list[dict]:
    q = session.query(ScopeBinding)
    if caller_key:
        q = q.filter_by(caller_key=caller_key)
    return [b.toDict() for b in q.order_by(ScopeBinding.caller_key).all()]


# ─── Resolution + enforcement ────────────────────────────────────────────────

def current_caller_key() -> Optional[str]:
    """The opaque caller identity for default-binding lookup, when known.

    Resolution order: the ``X-FD-Caller`` request header (set by the gateway
    / panel for authenticated callers — read from the FastMCP request context
    when serving over HTTP), then ``FD_MCP_CALLER_KEY`` (per-deployment
    integrations), else None — no default applies and retrieval runs
    unscoped.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        header = (get_http_headers() or {}).get(CALLER_HEADER, "").strip()
        if header:
            return header
    except Exception:  # noqa: BLE001 — stdio/non-HTTP contexts have no headers
        pass
    env = os.environ.get(CALLER_KEY_ENV, "").strip()
    return env or None


def resolve_scope(session: Session, explicit: Optional[str] = None,
                  caller_key: Optional[str] = None) -> Optional[dict]:
    """The scope in effect for this call, or None to run unscoped.

    Precedence (spec「Callers may bind a default scope」): an explicit
    parameter always wins — including the reserved ``unscoped`` name, which
    escapes even a bound default; otherwise the caller's default binding
    applies; otherwise None. A missing explicit scope raises
    ``ScopeNotFound`` (explicit error naming it, never an empty result); a
    dangling default binding (scope deleted) degrades to unscoped.
    """
    if caller_key is None:
        caller_key = current_caller_key()
    name = (explicit or "").strip()
    if not name:
        if not caller_key:
            return None
        binding = session.get(ScopeBinding, caller_key)
        if binding is None:
            return None
        name = binding.scope_name
    if name.lower() in RESERVED_NAMES:
        return None  # explicit unscoped escape
    row = session.query(Scope).filter_by(name=name).first()
    if row is None:
        if explicit:  # only an explicit reference is a hard error
            raise ScopeNotFound(name)
        logger.info("caller %r bound to deleted scope %r; running unscoped",
                    caller_key, name)
        return None
    return {"name": row.name, "rules": normalize_rules(row.rules),
            "description": row.description}


def _row_value(row: dict, *keys: str) -> Optional[str]:
    for k in keys:
        v = row.get(k)
        if v:
            return str(v)
    return None


def filter_result_rows(rows: list[dict], rules: dict[str, list[str]]) -> list[dict]:
    """Keep rows admitted by EVERY non-empty allow-list (empty = wildcard)."""
    out = []
    for row in rows:
        if rules["source_dbs"]:
            source = _row_value(row, "source_db")
            if source is None or source not in rules["source_dbs"]:
                continue
        if rules["domains"]:
            domain = _row_value(row, "domain", "category")
            if domain is None or domain not in rules["domains"]:
                continue
        if rules["semantic_codes"]:
            code = _row_value(row, "code", "semantic_code")
            if code is None or code not in rules["semantic_codes"]:
                continue
        if rules["native_codes"]:
            native = _row_value(row, "native_code")
            if native is None or native not in rules["native_codes"]:
                continue
        out.append(row)
    return out


def out_of_scope_payload(scope: dict, detail: str = "") -> dict:
    """The explicit out-of-scope response (spec「Explicit scope on read」)."""
    return {
        "error": "out_of_scope",
        "detail": detail or "the requested indicator is outside the scope in effect",
        "scope": scope_metadata(scope),
    }


def check_read_scope(scope: Optional[dict], *, code: Optional[str] = None,
                     category: Optional[str] = None, source_db: Optional[str] = None,
                     native_code: Optional[str] = None) -> Optional[dict]:
    """Violation payload when the indicator is outside the scope, else None."""
    if scope is None:
        return None
    rules = scope["rules"]
    reasons: list[str] = []
    if rules["source_dbs"] and (source_db is None or source_db not in rules["source_dbs"]):
        reasons.append(f"source_db {source_db!r} not in scope source_dbs")
    if rules["domains"] and (category is None or category not in rules["domains"]):
        reasons.append(f"domain {category!r} not in scope domains")
    if rules["semantic_codes"] and (code is None or code not in rules["semantic_codes"]):
        reasons.append(f"code {code!r} not in scope semantic_codes")
    if rules["native_codes"] and (native_code is None or native_code not in rules["native_codes"]):
        reasons.append(f"native_code {native_code!r} not in scope native_codes")
    if not reasons:
        return None
    return out_of_scope_payload(scope, "; ".join(reasons))


def scope_metadata(scope: Optional[dict]) -> Optional[dict]:
    """Name + definition summary attached to every scoped response."""
    if scope is None:
        return None
    rules = scope["rules"]
    parts = []
    for dim in RULE_DIMENSIONS:
        values = rules[dim]
        if values:
            shown = ", ".join(values[:5]) + (f" … (+{len(values) - 5})" if len(values) > 5 else "")
            parts.append(f"{dim}: {shown}")
        else:
            parts.append(f"{dim}: any")
    return {"name": scope["name"], "summary": " | ".join(parts)}


def domain_scope_gate(session: Session, scope_param: Optional[str],
                      tool_source_dbs: list[str]) -> tuple[Optional[dict], Optional[dict]]:
    """Shared gate for the single-source domain search tools (design D4).

    A domain ILIKE tool IS one source ("源即 scope"): its scope obligations
    reduce to — unknown scope → explicit error naming it (spec「Unknown scope
    errors clearly」); scope excludes this tool's source → explainable empty
    result (never a silent empty); scope admits it → serve everything (the
    tool cannot leak another source). Returns ``(scope_meta, gate_response)``;
    ``gate_response`` is not None when the call must not proceed.
    """
    resolved = resolve_scope(session, scope_param)
    if resolved is None:
        return None, None
    allowed = resolved["rules"]["source_dbs"]
    if allowed and not any(s in allowed for s in tool_source_dbs):
        return None, {
            "results": [], "count": 0, "truncated": False,
            "status": "out_of_scope",
            "note": ("this tool's source is outside the scope in effect — "
                     "empty by constraint, not for lack of data"),
            "scope": scope_metadata(resolved),
        }
    return scope_metadata(resolved), None


# ─── Hit statistics (design D6) ──────────────────────────────────────────────

def record_hit(session: Session, scope_name: str, results_returned: int) -> None:
    """Increment the per-(scope, day) counters; scoped retrievals only."""
    if not scope_name:
        return
    day = _dt.date.today().isoformat()
    stat = session.get(ScopeStat, {"scope_name": scope_name, "day": day})
    if stat is None:
        stat = ScopeStat(scope_name=scope_name, day=day, calls=0, results_returned=0)
        session.add(stat)
    stat.calls = (stat.calls or 0) + 1
    stat.results_returned = (stat.results_returned or 0) + max(0, int(results_returned))
    session.commit()


def scope_stats(session: Session, scope_name: str, days: int = 30) -> dict:
    """Per-day hit counters for one scope, most recent first."""
    if session.query(Scope).filter_by(name=scope_name).first() is None:
        raise ScopeNotFound(scope_name, "no statistics")
    rows = (
        session.query(ScopeStat)
        .filter_by(scope_name=scope_name)
        .order_by(ScopeStat.day.desc())
        .limit(max(1, int(days)))
        .all()
    )
    return {
        "scope_name": scope_name,
        "days": [s.toDict() for s in rows],
        "total_calls": sum(s.calls or 0 for s in rows),
        "total_results_returned": sum(s.results_returned or 0 for s in rows),
    }

"""find_canonical_replacement: same-entity_type constraint (no cross-domain aliases).

The stock OHLCV family (price.open/close/...) was deprecated in the System-B
migration with no stock replacement registered; the name_zh matcher used to
alias them onto crypto/index twins (CRYPTO_OPEN for stock price.open). A
deprecated stock concept must get either a same-type replacement or none.
"""
from __future__ import annotations

from fd_open_data_mcp.entities.resolver import find_canonical_replacement
from fd_open_data_mcp.models import Concept


def _mk(session, cid, code, entity_type, name_zh, deprecated=False):
    c = Concept(
        id=cid,
        code=code,
        entity_type=entity_type,
        name_zh=name_zh,
        frequency="daily",
        deprecated=deprecated,
    )
    session.add(c)
    return c


def test_same_type_replacement_wins_over_cross_domain(session):
    _mk(session, 1, "price.open", "stock", "开盘价", deprecated=True)
    _mk(session, 2, "CRYPTO_OPEN", "crypto", "开盘价")
    assert find_canonical_replacement(session, session.get(Concept, 1)) is None


def test_same_type_live_concept_is_returned(session):
    _mk(session, 1, "price.open", "stock", "开盘价", deprecated=True)
    _mk(session, 2, "CRYPTO_OPEN", "crypto", "开盘价")
    _mk(session, 3, "price.open.v2", "stock", "开盘价")
    assert find_canonical_replacement(session, session.get(Concept, 1)).id == 3


def test_symbol_to_entity_typed_migration_fallback_preserved(session):
    _mk(session, 1, "PRICE_CLOSE", "symbol", "收盘价", deprecated=True)
    _mk(session, 2, "price.close", "stock", "收盘价")
    assert find_canonical_replacement(session, session.get(Concept, 1)).id == 2


def test_no_live_name_mate_returns_none(session):
    _mk(session, 1, "price.close", "stock", "收盘价", deprecated=True)
    assert find_canonical_replacement(session, session.get(Concept, 1)) is None


def test_deprecated_name_mate_is_not_suggested(session):
    _mk(session, 1, "price.open", "stock", "开盘价", deprecated=True)
    _mk(session, 2, "price.open.old", "stock", "开盘价", deprecated=True)
    assert find_canonical_replacement(session, session.get(Concept, 1)) is None

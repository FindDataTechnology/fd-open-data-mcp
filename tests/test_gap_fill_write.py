"""Insert-only gap-fill writes (design D2).

The contract: gap filling adds missing dates and never rewrites a stored value.
That is what keeps a backtest reproducible — a forward-adjusted series is
rewritten wholesale upstream after every dividend, so a fill that "refreshed"
values would silently change the meaning of history under an existing run.

The assertions therefore check the stored value *after* an attempted overwrite,
not merely that a write happened.
"""

from __future__ import annotations

import pytest

from fd_open_data_mcp.fetch.cache import (
    read_cache_range,
    write_cache_range,
    write_cache_range_new_only,
)
from fd_open_data_mcp.models import Concept

T, EID = "stock", 42
SRC = "akshare"
SRC_B = "polygon"


@pytest.fixture
def cid(session) -> int:
    """A real Concept row: observations carry a FK to it, so a bare id fails."""
    c = Concept(code="price.close", entity_type=T, frequency="daily",
                unit="currency", verified=True)
    session.add(c)
    session.commit()
    return c.id


def _stored(session, cid, start="2000-01-01", end="2099-12-31") -> dict[str, str]:
    return {r.date: r.value for r in read_cache_range(session, cid, T, EID, start, end)}


def test_existing_value_is_not_overwritten(session, cid) -> None:
    """The core guarantee: a fill must not change what is already stored."""
    write_cache_range(session, cid, T, EID, {"2026-01-05": 10.0}, "currency", SRC)
    changed = write_cache_range_new_only(
        session, cid, T, EID, {"2026-01-05": 12.0}, "currency", SRC
    )
    assert changed == 0, "nothing new was inserted"
    assert _stored(session, cid) == {"2026-01-05": "10.0"}, "the old value must survive"


def test_missing_dates_are_added_alongside_existing_ones(session, cid) -> None:
    write_cache_range(session, cid, T, EID, {"2026-01-05": 10.0}, "currency", SRC)
    inserted = write_cache_range_new_only(
        session, cid, T, EID,
        {"2026-01-05": 12.0, "2026-01-06": 11.0, "2026-01-07": 13.0},
        "currency", SRC,
    )
    assert inserted == 2, "only the two absent dates are new"
    assert _stored(session, cid) == {
        "2026-01-05": "10.0",  # untouched
        "2026-01-06": "11.0",
        "2026-01-07": "13.0",
    }


def test_second_fill_is_a_no_op(session, cid) -> None:
    """Idempotence is what makes repeated gap filling free."""
    rows = {"2026-01-05": 1.0, "2026-01-06": 2.0}
    assert write_cache_range_new_only(session, cid, T, EID, rows, "currency", SRC) == 2
    assert write_cache_range_new_only(session, cid, T, EID, rows, "currency", SRC) == 0
    assert _stored(session, cid) == {"2026-01-05": "1.0", "2026-01-06": "2.0"}


def test_empty_rows_touches_nothing(session, cid) -> None:
    assert write_cache_range_new_only(session, cid, T, EID, {}, "currency", SRC) == 0
    assert _stored(session, cid) == {}


def test_other_source_rows_are_untouched(session, cid) -> None:
    """Per-source coexistence: filling via one source must not clobber another."""
    write_cache_range(session, cid, T, EID, {"2026-01-05": 10.0}, "currency", SRC_B)
    write_cache_range_new_only(session, cid, T, EID, {"2026-01-05": 99.0}, "currency", SRC)
    stored = _stored(session, cid)
    assert stored["2026-01-05"] in {"10.0", "99.0"}  # both rows exist; read picks one
    from fd_open_data_mcp.fetch.cache import _read_base_range

    rows = _read_base_range(session, cid, T, EID, "2026-01-05", "2026-01-05")
    assert {r.source_used for r in rows} == {SRC, SRC_B}


def test_the_upsert_variant_still_refreshes(session, cid) -> None:
    """Contrast case: the first-fetch path *does* update (today's cell needs it)."""
    write_cache_range(session, cid, T, EID, {"2026-01-05": 10.0}, "currency", SRC)
    write_cache_range(session, cid, T, EID, {"2026-01-05": 12.0}, "currency", SRC)
    assert _stored(session, cid) == {"2026-01-05": "12.0"}


def test_fill_after_upsert_keeps_the_refreshed_value(session, cid) -> None:
    write_cache_range(session, cid, T, EID, {"2026-01-05": 12.0}, "currency", SRC)
    write_cache_range_new_only(session, cid, T, EID, {"2026-01-05": 1.0}, "currency", SRC)
    assert _stored(session, cid) == {"2026-01-05": "12.0"}


@pytest.mark.parametrize("value", [0.0, -1.5, 1e9])
def test_numeric_edge_values_stored_as_strings(session, cid, value) -> None:
    write_cache_range_new_only(session, cid, T, EID, {"2026-02-02": value}, "currency", SRC)
    assert _stored(session, cid) == {"2026-02-02": str(value)}

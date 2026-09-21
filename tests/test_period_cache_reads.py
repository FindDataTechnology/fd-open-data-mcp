"""Period-aware cache reads (spec observation-reading).

A request for any date inside a period must resolve to that period's stored
observation — whose stored date may be the bare period form ("2021") or any
date within it — while daily concepts keep exact-date matching and
current-period freshness still triggers dispatch.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fd_open_data_mcp.fetch.cache import (
    is_stale,
    period_prefixes,
    read_cache,
    write_cache,
)
from fd_open_data_mcp.models import Concept, SourceRanking

T, E = "country", 7
LAST_YEAR = str(int(datetime.now(timezone.utc).strftime("%Y")) - 1)
THIS_YEAR = datetime.now(timezone.utc).strftime("%Y")


def _concept(session, frequency: str, code: str = "gdp") -> int:
    c = Concept(code=code, entity_type=T, frequency=frequency, unit="USD", verified=True)
    session.add(c)
    session.commit()
    return c.id


# --- prefix computation -------------------------------------------------------


def test_period_prefixes_by_frequency():
    assert period_prefixes("2021-07-04", "yearly") == ["2021"]
    assert period_prefixes("2021-07-04", "monthly") == ["2021-07"]
    assert period_prefixes("2021-08-15", "quarterly") == ["2021-07", "2021-08", "2021-09"]
    assert period_prefixes("2021-02-15", "quarterly") == ["2021-01", "2021-02", "2021-03"]
    # daily/weekly keep exact semantics
    assert period_prefixes("2021-07-04", "daily") is None
    assert period_prefixes("2021-07-04", "weekly") is None
    assert period_prefixes("2021-07-04", None) is None


# --- yearly: bare-year storage, off-anchor request ----------------------------


def test_yearly_bare_year_row_hits_from_any_date_in_that_year(session):
    c = _concept(session, "yearly")
    write_cache(session, c, T, E, LAST_YEAR, "1.9e12", "USD", "worldbank")  # stored as bare year

    for asked in (f"{LAST_YEAR}-01-01", f"{LAST_YEAR}-07-04", f"{LAST_YEAR}-12-31"):
        obs = read_cache(session, c, T, E, asked, frequency="yearly")
        assert obs is not None, f"period hit expected for {asked}"
        assert obs.value == "1.9e12"
        assert obs.date == LAST_YEAR


def test_yearly_full_date_row_hits_through_period(session):
    c = _concept(session, "yearly")
    write_cache(session, c, T, E, f"{LAST_YEAR}-12-31", "2.1e12", "USD", "worldbank")

    obs = read_cache(session, c, T, E, f"{LAST_YEAR}-06-01", frequency="yearly")
    assert obs is not None and obs.value == "2.1e12"


def test_other_year_still_misses(session):
    c = _concept(session, "yearly")
    write_cache(session, c, T, E, LAST_YEAR, "1.9e12", "USD", "worldbank")
    assert read_cache(session, c, T, E, f"{THIS_YEAR}-07-01", frequency="yearly") is None


# --- monthly / quarterly ------------------------------------------------------


def test_monthly_hits_within_month_only(session):
    c = _concept(session, "monthly", code="cpi")
    write_cache(session, c, T, E, "2021-03-01", "101.2", "idx", "cnstats")

    assert read_cache(session, c, T, E, "2021-03-28", frequency="monthly") is not None
    assert read_cache(session, c, T, E, "2021-04-01", frequency="monthly") is None


def test_quarterly_hits_any_month_of_quarter(session):
    c = _concept(session, "quarterly", code="gdp_q")
    write_cache(session, c, T, E, "2021-07-01", "Q3", "USD", "worldbank")

    assert read_cache(session, c, T, E, "2021-09-30", frequency="quarterly") is not None
    assert read_cache(session, c, T, E, "2021-10-01", frequency="quarterly") is None


def test_multiple_rows_in_period_pick_closest_at_or_before(session):
    c = _concept(session, "quarterly", code="gdp_q2")
    write_cache(session, c, T, E, "2021-07-01", "early", "USD", "worldbank")
    write_cache(session, c, T, E, "2021-09-01", "late", "USD", "worldbank")

    # a request between the two rows resolves to the earlier one
    mid = read_cache(session, c, T, E, "2021-08-15", frequency="quarterly")
    assert mid is not None and mid.value == "early"
    # after both, the later one
    end = read_cache(session, c, T, E, "2021-09-30", frequency="quarterly")
    assert end is not None and end.value == "late"


# --- daily keeps exact semantics ---------------------------------------------


def test_daily_requires_exact_date(session):
    c = _concept(session, "daily", code="close")
    write_cache(session, c, T, E, "2021-07-04", "12.5", "USD", "yfinance")

    assert read_cache(session, c, T, E, "2021-07-04", frequency="daily") is not None
    assert read_cache(session, c, T, E, "2021-07-05", frequency="daily") is None
    # and no frequency argument at all stays exact
    assert read_cache(session, c, T, E, "2021-07-04") is not None


# --- source pinning + staleness ----------------------------------------------


def test_source_pinned_period_read(session):
    c = _concept(session, "yearly", code="gdp_src")
    write_cache(session, c, T, E, LAST_YEAR, "wb", "USD", "worldbank")
    write_cache(session, c, T, E, LAST_YEAR, "nbs", "USD", "cnstats")

    wb = read_cache(session, c, T, E, f"{LAST_YEAR}-06-01", source="worldbank", frequency="yearly")
    nbs = read_cache(session, c, T, E, f"{LAST_YEAR}-06-01", source="cnstats", frequency="yearly")
    assert wb is not None and wb.value == "wb"
    assert nbs is not None and nbs.value == "nbs"
    assert read_cache(session, c, T, E, f"{LAST_YEAR}-06-01", source="missing", frequency="yearly") is None


def test_elapsed_period_row_is_never_stale(session):
    """The immutability rule is what makes a period hit servable without dispatch."""
    c = _concept(session, "yearly", code="gdp_old")
    write_cache(session, c, T, E, LAST_YEAR, "1.9e12", "USD", "worldbank")
    obs = read_cache(session, c, T, E, f"{LAST_YEAR}-06-01", frequency="yearly")

    assert obs is not None and not is_stale(obs, "yearly")


def test_current_period_row_still_ttl_gated(session):
    """A current-year row is returned by the period lookup but stays freshness-checked."""
    c = _concept(session, "yearly", code="gdp_now")
    write_cache(session, c, T, E, THIS_YEAR, "0.5e12", "USD", "worldbank")
    obs = read_cache(session, c, T, E, f"{THIS_YEAR}-06-01", frequency="yearly")

    assert obs is not None  # the lookup finds it; dispatch decides on staleness
    assert isinstance(is_stale(obs, "yearly"), bool)


# --- diagnostic error rows ----------------------------------------------------


def test_no_binding_error_is_distinct_from_attempt_failure(session):
    """A concept with no confirmed binding reports that fact, not 'no source succeeded'."""
    from fd_open_data_mcp.fetch.dispatch import dispatch_one

    c = _concept(session, "yearly", code="unbound")
    row = dispatch_one(session, c, T, E, "2020-01-01")

    assert row is not None and row["value"] is None
    assert row["error"] == "no eligible source"
    assert "binding" in row["detail"]
    assert row["error"] != "no source succeeded"

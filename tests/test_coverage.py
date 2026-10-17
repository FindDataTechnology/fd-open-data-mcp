"""Coverage judgment (design D1) — the pure function behind gap filling.

These tests are written against *real* date shapes rather than a convenient
continuum: a price series only has points on trading days, so a window of
weekdays must read as complete even though it is full of calendar holes. A
coverage function judged against "one point per calendar day" would report
every weekend as a gap and make gap filling fetch constantly.

The four scenarios here are the four the contract names: boundary-missing,
interior hole, cold series, complete.
"""

from __future__ import annotations

import datetime as dt

import pytest

from fd_open_data_mcp.fetch.coverage import DEFAULT_MIN_HOLE_DAYS, Coverage, coverage


def _span(a: str, b: str, weekdays_only: bool = True) -> list[str]:
    """Dates a series has points on. Defaults to weekdays (a real price series)."""
    out = []
    d = dt.date.fromisoformat(a)
    while d <= dt.date.fromisoformat(b):
        if not weekdays_only or d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


WEEK = _span("2026-01-01", "2026-06-30")


# ---------------------------------------------------------------------------
# the four contract scenarios
# ---------------------------------------------------------------------------


def test_boundary_missing_reports_both_outer_stretches() -> None:
    """Cache covers Jan–Jun; the request reaches a year further back and forward."""
    c = coverage(WEEK, "2025-01-01", "2026-10-17")
    assert c.gaps == (("2025-01-01", "2025-12-31"), ("2026-07-01", "2026-10-17"))
    assert not c.cold
    # The covered middle is NOT re-fetched: it appears in no gap.
    assert all(not ("2026-01-01" <= a and b <= "2026-06-30") for a, b in c.gaps)


def test_interior_hole_reports_only_the_hole() -> None:
    holey = [d for d in WEEK if not d.startswith("2026-03")]
    c = coverage(holey, "2026-01-01", "2026-06-30")
    assert len(c.gaps) == 1
    assert c.gaps[0][0].startswith("2026-02")
    assert c.gaps[0][1].startswith("2026-03")


def test_cold_series_reports_the_whole_window() -> None:
    """With no points there is no evidence: the conservative answer is 'missing'."""
    c = coverage([], "2025-01-01", "2026-10-17")
    assert c.cold
    assert c.span_start is None and c.span_end is None
    assert c.gaps == (("2025-01-01", "2026-10-17"),)


def test_complete_window_reports_no_gaps() -> None:
    c = coverage(WEEK, "2026-01-01", "2026-06-30")
    assert c.complete
    assert c.gaps == ()


# ---------------------------------------------------------------------------
# the judgment must survive real (sparse) date shapes
# ---------------------------------------------------------------------------


def test_weekday_series_is_complete_despite_calendar_holes() -> None:
    """The whole point of judging against the data: weekends are not gaps."""
    c = coverage(WEEK, "2026-01-01", "2026-06-30")
    assert c.complete, "a real trading calendar must not read as full of holes"


def test_halt_longer_than_threshold_is_a_gap() -> None:
    halted = [d for d in WEEK if not ("2026-03-02" <= d <= "2026-03-06")]
    c = coverage(halted, "2026-01-01", "2026-06-30")
    assert c.gaps, "a five-day halt is a real hole and must be fetched"


def test_single_missing_day_is_not_a_gap() -> None:
    """One absent day is a non-trading day or an artifact a range fetch won't fill."""
    halted = [d for d in WEEK if d != "2026-03-03"]
    c = coverage(halted, "2026-01-01", "2026-06-30")
    assert c.complete, "an isolated missing day must not trigger a fetch"


def test_threshold_is_configurable() -> None:
    """Lowering min_hole_days makes a one-day hole visible — it is a knob, not a law."""
    halted = [d for d in WEEK if d != "2026-03-03"]
    assert coverage(halted, "2026-01-01", "2026-06-30").complete
    strict = coverage(halted, "2026-01-01", "2026-06-30", min_hole_days=1)
    assert not strict.complete
    assert DEFAULT_MIN_HOLE_DAYS > 1


# ---------------------------------------------------------------------------
# determinism + input handling
# ---------------------------------------------------------------------------


def test_same_input_gives_same_answer() -> None:
    holey = [d for d in WEEK if not d.startswith("2026-03")]
    first = coverage(holey, "2025-06-01", "2026-06-30")
    second = coverage(list(reversed(holey)), "2025-06-01", "2026-06-30")
    assert first == second, "order of input dates must not change the judgment"


def test_duplicate_dates_do_not_distort_density() -> None:
    doubled = WEEK + WEEK[:10]
    assert coverage(doubled, "2026-01-01", "2026-06-30").complete


def test_inverted_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="after end"):
        coverage(WEEK, "2026-06-30", "2026-01-01")


def test_span_reports_the_series_own_bounds() -> None:
    """Span is over *all* dates, so it can extend beyond the window."""
    c = coverage(WEEK, "2026-03-01", "2026-04-30")
    assert c.span_start == min(WEEK)
    assert c.span_end == max(WEEK)
    assert c.span_start < "2026-03-01" and c.span_end > "2026-04-30"


def test_window_wider_than_series_but_inside_span_is_complete() -> None:
    """A window reaching past the series' ends but within its span needs nothing."""
    short = _span("2026-02-01", "2026-02-28")
    c = coverage(short, "2026-02-01", "2026-02-28")
    assert c.complete


def test_coverage_is_frozen_value_type() -> None:
    c = coverage(WEEK, "2026-01-01", "2026-06-30")
    assert isinstance(c, Coverage)
    with pytest.raises(Exception):
        c.gaps = ()  # type: ignore[misc]

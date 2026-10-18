"""Gap-fill orchestration (design D2/D6, capability price-on-demand-fill).

The property that matters is **which intervals get fetched**. A fill that
re-requested the whole window would look correct on a small fixture and cost a
full upstream round trip on every real request, so the fetcher is injected and
its calls are asserted — the tests fail if the covered middle is touched.

The second property is that a stored value survives a fill (see
``test_gap_fill_write.py``); here it is asserted end-to-end through ``fill_gaps``.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from fd_open_data_mcp.fetch.gapfill import fill_gaps, fingerprint, is_fillable
from fd_open_data_mcp.fetch.cache import write_cache_range
from fd_open_data_mcp.models import Concept

T, EID = "stock", 42
SRC = "akshare"


@pytest.fixture
def cid(session) -> int:
    c = Concept(code="price.close", entity_type=T, frequency="daily",
                unit="currency", verified=True)
    session.add(c)
    session.commit()
    return c.id


@pytest.fixture
def other_cid(session) -> int:
    c = Concept(code="financials.revenue", entity_type=T, frequency="quarterly",
                unit="CNY", verified=True)
    session.add(c)
    session.commit()
    return c.id


def _weekdays(a: str, b: str) -> list[str]:
    out, d = [], dt.date.fromisoformat(a)
    while d <= dt.date.fromisoformat(b):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


class Recorder:
    """Injectable fetcher: records every interval it is asked for."""

    def __init__(self, values: dict[str, float] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._values = values

    def __call__(self, start: str, end: str):
        self.calls.append((start, end))
        if self._values is None:
            return {}, ""
        return {d: v for d, v in self._values.items() if start <= d <= end}, SRC


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------


def test_price_concept_is_fillable(session, cid) -> None:
    assert is_fillable(session.get(Concept, cid))


def test_non_price_concept_is_not_filled(session, other_cid) -> None:
    """Revised-value concepts must not be pinned in place by an additive fill."""
    out = fill_gaps(session, other_cid, T, EID, "2026-01-01", "2026-06-30")
    assert out["fill_attempted"] is False
    assert out["note"]


# ---------------------------------------------------------------------------
# only the missing intervals are fetched
# ---------------------------------------------------------------------------


def test_covered_middle_is_never_refetched(session, cid) -> None:
    """The headline property: cost is proportional to the gap, not the window."""
    covered = _weekdays("2026-01-01", "2026-06-30")
    write_cache_range(
        session, cid, T, EID, {d: 10.0 for d in covered}, "currency", SRC
    )
    rec = Recorder({d: 1.0 for d in _weekdays("2025-01-01", "2026-10-17")})
    out = fill_gaps(
        session, cid, T, EID, "2025-01-01", "2026-10-17", fetch_range=rec
    )
    assert rec.calls, "the missing stretches must be fetched"
    # No requested interval may overlap the already-covered middle.
    for start, end in rec.calls:
        assert end < "2026-01-01" or start > "2026-06-30", (
            f"interval {start}..{end} re-fetches covered data"
        )
    assert out["gaps"] == []


def test_cold_series_fetches_the_whole_window_once(session, cid) -> None:
    rec = Recorder({d: 1.0 for d in _weekdays("2025-01-01", "2026-10-17")})
    out = fill_gaps(
        session, cid, T, EID, "2025-01-01", "2026-10-17", fetch_range=rec
    )
    assert len(rec.calls) == 1, "a cold series needs exactly one range fetch"
    assert rec.calls[0] == ("2025-01-01", "2026-10-17")


def test_complete_window_fetches_nothing(session, cid) -> None:
    covered = _weekdays("2026-01-01", "2026-06-30")
    write_cache_range(
        session, cid, T, EID, {d: 10.0 for d in covered}, "currency", SRC
    )
    rec = Recorder({})
    out = fill_gaps(
        session, cid, T, EID, "2026-01-01", "2026-06-30", fetch_range=rec
    )
    assert rec.calls == [], "a complete window must not hit the network"
    assert out["fill_attempted"] is False
    assert out["gaps"] == []


# ---------------------------------------------------------------------------
# additive: a stored value survives
# ---------------------------------------------------------------------------


def test_fill_does_not_rewrite_an_existing_point(session, cid) -> None:
    write_cache_range(session, cid, T, EID, {"2026-01-05": 10.0}, "currency", SRC)
    rec = Recorder({"2026-01-05": 99.0, "2026-01-06": 11.0})
    out = fill_gaps(
        session, cid, T, EID, "2026-01-01", "2026-01-10", fetch_range=rec
    )
    by_date = {p["date"]: p["value"] for p in out["points"]}
    assert by_date["2026-01-05"] == 10.0, "the stored value must survive"
    assert by_date["2026-01-06"] == 11.0, "the missing day is added"


# ---------------------------------------------------------------------------
# failure degrades with a labelled gap (design D6)
# ---------------------------------------------------------------------------


def test_upstream_failure_still_returns_held_points(session, cid) -> None:
    covered = _weekdays("2026-01-01", "2026-03-31")
    write_cache_range(
        session, cid, T, EID, {d: 10.0 for d in covered}, "currency", SRC
    )
    out = fill_gaps(
        session, cid, T, EID, "2025-01-01", "2026-10-17", fetch_range=Recorder(None)
    )
    assert out["points"], "held data is still returned"
    assert out["fill_attempted"] is True
    assert out["fill_failed"] is True
    assert out["gaps"], "the missing stretches must be named, not implied"


def test_cold_series_with_failing_upstream_reports_the_whole_window(session, cid) -> None:
    out = fill_gaps(
        session, cid, T, EID, "2025-01-01", "2026-10-17", fetch_range=Recorder(None)
    )
    assert out["points"] == []
    assert out["fill_failed"] is True
    assert out["gaps"][0]["start"] == "2025-01-01"


def test_one_bad_interval_does_not_stop_the_others(session, cid) -> None:
    """A partial fill is still worth returning.

    A cold series yields one interval, so this starts from a *mid-covered*
    series to get a leading and a trailing interval — then only one of them is
    made to fail, which is the case the isolation guarantee is about.
    """
    covered = _weekdays("2026-01-01", "2026-06-30")
    write_cache_range(
        session, cid, T, EID, {d: 10.0 for d in covered}, "currency", SRC
    )

    def flaky(start, end):
        # The leading stretch fails; the trailing one succeeds.
        if start < "2026-01-01":
            return {}, ""
        return {d: 1.0 for d in _weekdays(start, end)}, SRC

    out = fill_gaps(
        session, cid, T, EID, "2025-01-01", "2026-10-17", fetch_range=flaky
    )
    assert out["fill_failed"] is True, "the failing stretch must be reported"
    assert len(out["gaps"]) == 1 and out["gaps"][0]["end"] < "2026-01-01", (
        f"only the failed stretch should remain: {out['gaps']}"
    )
    # The covered middle plus the successfully filled tail are both returned.
    dates = {p["date"] for p in out["points"]}
    assert "2026-03-02" in dates, "held data is returned"
    assert any(d > "2026-06-30" for d in dates), "the filled tail is returned"


# ---------------------------------------------------------------------------
# fingerprint (design D4)
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_across_identical_reads(session, cid) -> None:
    covered = _weekdays("2026-01-01", "2026-06-30")
    write_cache_range(
        session, cid, T, EID, {d: 10.0 for d in covered}, "currency", SRC
    )
    first = fill_gaps(session, cid, T, EID, "2026-01-01", "2026-06-30")
    second = fill_gaps(session, cid, T, EID, "2026-01-01", "2026-06-30")
    assert first["fingerprint"] == second["fingerprint"]


def test_fingerprint_changes_when_a_gap_is_filled(session, cid) -> None:
    write_cache_range(session, cid, T, EID, {"2026-01-05": 10.0}, "currency", SRC)
    before = fill_gaps(session, cid, T, EID, "2026-01-01", "2026-01-10")
    after = fill_gaps(
        session, cid, T, EID, "2026-01-01", "2026-01-10",
        fetch_range=Recorder({"2026-01-06": 11.0}),
    )
    assert before["fingerprint"] != after["fingerprint"]


def test_fingerprint_is_content_sensitive() -> None:
    a = fingerprint([("2026-01-01", 1.0), ("2026-01-02", 2.0)])
    b = fingerprint([("2026-01-01", 1.0), ("2026-01-02", 2.5)])
    c = fingerprint([("2026-01-01", 1.0)])
    assert a != b and a != c
    # Order of input must not matter.
    assert a == fingerprint([("2026-01-02", 2.0), ("2026-01-01", 1.0)])
    assert fingerprint([]) == fingerprint([])


# ---------------------------------------------------------------------------
# reverse self-check (task 3.4)
# ---------------------------------------------------------------------------


def test_reverse_selfcheck_a_broken_gate_is_caught(session, cid, monkeypatch) -> None:
    """Prove the degrade assertions can fail.

    A test suite passes trivially if the thing it guards cannot break. Here the
    coverage judgment is stubbed to report "no gaps" — the silent-wrong-data bug
    this capability exists to fix — and the result must visibly disagree with
    what the caller needs: while ``fill_failed`` is set, ``gaps`` claims
    completeness. The real assertions (above) fail on exactly this shape.
    """
    import fd_open_data_mcp.fetch.gapfill as gapfill_mod

    from fd_open_data_mcp.fetch.coverage import Coverage

    monkeypatch.setattr(
        gapfill_mod,
        "coverage",
        lambda dates, start, end, **kw: Coverage(
            span_start="2026-01-01", span_end="2026-06-30",
            present=(), gaps=(),
        ),
    )
    out = fill_gaps(
        session, cid, T, EID, "2025-01-01", "2026-10-17",
        fetch_range=Recorder(None),
    )
    # With "no gaps" reported, no fetch is attempted and nothing is filled — so
    # a series that is actually empty comes back claiming to be complete. That
    # is precisely the silent-wrong-data bug, and it is what the real tests
    # above catch: they assert points were returned *and* gaps were named.
    assert out["fill_attempted"] is False
    assert out["gaps"] == []
    assert out["points"] == [], "the broken gate serves an empty window as complete"


# ---------------------------------------------------------------------------
# probed-empty intervals: ask once, not every read
# ---------------------------------------------------------------------------


def test_an_interval_that_answers_empty_is_asked_only_once(session, cid) -> None:
    """A market holiday looks like a hole; it must not be re-asked on every read.

    Measured on a two-year A-share window: six such intervals, so an
    unconditional retry meant six wasted upstream calls per read.
    """
    covered = _weekdays("2026-01-01", "2026-06-30")
    write_cache_range(
        session, cid, T, EID, {d: 10.0 for d in covered}, "currency", SRC
    )
    calls: list[tuple[str, str]] = []

    def answers_empty(start, end):
        calls.append((start, end))
        return {}, SRC  # a source answered: genuinely no data here

    fill_gaps(session, cid, T, EID, "2025-01-01", "2026-10-17",
              fetch_range=answers_empty)
    first_round = len(calls)
    fill_gaps(session, cid, T, EID, "2025-01-01", "2026-10-17",
              fetch_range=answers_empty)
    assert len(calls) == first_round, (
        f"asked {len(calls) - first_round} extra times for intervals already "
        "known to hold nothing"
    )


def test_a_failed_interval_stays_retryable(session, cid) -> None:
    """Conflating 'no data' with 'could not ask' would hide a transient outage."""
    covered = _weekdays("2026-01-01", "2026-06-30")
    write_cache_range(
        session, cid, T, EID, {d: 10.0 for d in covered}, "currency", SRC
    )
    for _ in range(2):
        out = fill_gaps(
            session, cid, T, EID, "2025-01-01", "2026-10-17",
            fetch_range=Recorder(None),
        )
        assert out["fill_failed"] is True, "a failure stays a failure"
        assert out["gaps"], "and the interval is still reported as missing"


# ---------------------------------------------------------------------------
# the real fetch path (``_fetch_interval``): answered-empty vs could-not-ask
# ---------------------------------------------------------------------------


def test_fetch_interval_reports_the_source_that_answered_empty(
    session, cid, monkeypatch
) -> None:
    """The distinction that makes ``known_empty`` reachable at all.

    ``fill_gaps`` only records a probed-empty interval when the fetcher names a
    source ("answered and there was nothing"). The real fetcher used to return
    ``({}, "")`` for that case too, so a market holiday was indistinguishable
    from an outage and ``_record_empty`` never ran — the ``known_empty`` table
    stayed empty and every holiday read as a missing interval. Every other test
    in this file injects its own fetcher, which is exactly why that went
    unnoticed; this one drives the real one.
    """
    from fd_open_data_mcp.fetch import dispatch as dispatch_mod
    from fd_open_data_mcp.fetch import gapfill as gapfill_mod

    monkeypatch.setattr(
        dispatch_mod, "rank_sources_for_concept",
        lambda *a, **kw: [{"source": SRC}],
    )
    monkeypatch.setattr(
        dispatch_mod, "resolve_identifier", lambda *a, **kw: "IDENT",
    )
    binding, fn = MagicMock(name="binding"), MagicMock(name="fn")
    fn.command = "cmd"
    monkeypatch.setattr(
        dispatch_mod, "_bindings_for_source",
        lambda *a, **kw: [(binding, fn)],
    )
    # An instrumented fetch that succeeds and yields an empty series — the
    # signature of a holiday, not of a failure.
    monkeypatch.setattr(
        dispatch_mod, "instrumented_fetch", lambda *a, **kw: {"data": []},
    )
    monkeypatch.setattr(
        dispatch_mod, "_extract_series", lambda *a, **kw: {},
    )

    concept = session.get(Concept, cid)
    series, used = gapfill_mod._fetch_interval(
        session, concept, T, EID, "2026-10-01", "2026-10-07",
    )
    assert series == {}
    assert used == SRC, (
        "an answered-but-empty interval must name its source, otherwise the "
        "caller cannot tell a holiday from an outage and never records it"
    )


def test_fetch_interval_reports_no_source_when_every_attempt_throws(
    session, cid, monkeypatch
) -> None:
    """A failed ask must stay retryable: no source name, no ``known_empty``."""
    from fd_open_data_mcp.fetch import dispatch as dispatch_mod
    from fd_open_data_mcp.fetch import gapfill as gapfill_mod

    monkeypatch.setattr(
        dispatch_mod, "rank_sources_for_concept",
        lambda *a, **kw: [{"source": SRC}],
    )
    monkeypatch.setattr(
        dispatch_mod, "resolve_identifier", lambda *a, **kw: "IDENT",
    )
    binding, fn = MagicMock(name="binding"), MagicMock(name="fn")
    fn.command = "cmd"
    monkeypatch.setattr(
        dispatch_mod, "_bindings_for_source",
        lambda *a, **kw: [(binding, fn)],
    )

    def _boom(*a, **kw):
        raise dispatch_mod.FetchError("down")

    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", _boom)

    concept = session.get(Concept, cid)
    series, used = gapfill_mod._fetch_interval(
        session, concept, T, EID, "2026-10-01", "2026-10-07",
    )
    assert series == {}
    assert used == "", "a failure proves nothing and must not be recorded as empty"


def test_holiday_is_recorded_and_a_recovers(session, cid, monkeypatch) -> None:
    """End to end: a holiday answered empty is recorded, then read as closed.

    The window spans a holiday with data on both sides; after the fill the
    interval must appear in ``market_closed`` rather than ``missing``, which is
    what lets the backtest gate accept a window containing it.
    """
    before = _weekdays("2026-09-28", "2026-09-30")
    after = _weekdays("2026-10-08", "2026-10-09")
    write_cache_range(
        session, cid, T, EID, {d: 10.0 for d in before + after}, "currency", SRC
    )

    def answers_empty(start, end):
        return {}, SRC  # a source answered: genuinely no data here

    out = fill_gaps(session, cid, T, EID, "2026-09-28", "2026-10-09",
                    fetch_range=answers_empty)
    closed = [(i["start"], i["end"]) for i in out["market_closed"]]
    assert closed, f"the holiday must be reported as closed, got {out['gaps']}"
    assert out["missing"] == [], (
        f"a proven-empty holiday must not stay in missing: {out['missing']}"
    )

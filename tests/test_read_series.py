"""read_series tool (spec observation-reading).

Cache-only range reads: validation, entity applicability, dedup by preferred
source, truncation reporting, and — the contract that matters — no upstream
dispatch on any path.
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.fetch.cache import write_cache
from fd_open_data_mcp.models import Concept

T, E = "country", 42


@pytest.fixture
def concept(session):
    c = Concept(code="series_demo", entity_type=T, frequency="yearly", unit="USD", verified=True)
    session.add(c)
    session.commit()
    return c.id


def _call(concept_id, **overrides):
    """Invoke the MCP tool function (not the transport) with defaults."""
    from fd_open_data_mcp.server import read_series

    args = dict(concept_id=concept_id, entity_type=T, entity_id=E,
                start="2018-01-01", end="2022-12-31")
    args.update(overrides)
    return read_series(**args)


def test_returns_stored_points_in_order(session, concept):
    for year, value in (("2019", "1.9e12"), ("2020", "2.0e12"), ("2021", "2.1e12")):
        write_cache(session, concept, T, E, year, value, "USD", "worldbank")

    # 窗口必须落在数据实际覆盖的范围内：2018-2022 里 2018/2022 确实没有点，
    # 那种情况下**应当**带 coverage 的 note（见 test_year_series_partial_window_is_noted）。
    out = _call(concept, start="2019", end="2021")

    assert out["count"] == 3
    assert [p["date"] for p in out["points"]] == ["2019", "2020", "2021"]
    assert all(p["source_used"] == "worldbank" for p in out["points"])
    assert "note" not in out
    assert out["coverage"]["complete"] is True


def test_empty_window_is_a_note_not_an_error(session, concept):
    out = _call(concept)

    assert out["count"] == 0 and out["points"] == []
    assert "coverage gap" in out["note"]


def test_inverted_window_is_rejected(session, concept):
    with pytest.raises(ValueError, match="after end"):
        _call(concept, start="2022-01-01", end="2021-01-01")


def test_missing_bounds_rejected(session, concept):
    with pytest.raises(ValueError, match="required"):
        _call(concept, start="")


def test_entity_type_mismatch_is_rejected(session, concept):
    with pytest.raises(Exception) as ei:
        _call(concept, entity_type="stock")
    assert "applies to entity_type" in str(ei.value)


def test_deprecated_concept_is_rejected(session, concept):
    row = session.get(Concept, concept)
    row.deprecated = True
    session.commit()

    with pytest.raises(Exception):
        _call(concept)


def test_preferred_source_wins_per_point(session, concept):
    from fd_open_data_mcp.models import SourceRanking

    write_cache(session, concept, T, E, "2020", "wb", "USD", "worldbank")
    write_cache(session, concept, T, E, "2020", "nbs", "USD", "cnstats")
    session.add(SourceRanking(source="worldbank", concept_id=concept, quality=0.9,
                              accessibility=0.9, freshness_fit=0.9))
    session.add(SourceRanking(source="cnstats", concept_id=concept, quality=0.1,
                              accessibility=0.1, freshness_fit=0.1))
    session.commit()

    out = _call(concept)
    assert out["count"] == 1 and out["points"][0]["source_used"] == "worldbank"


def test_oversized_window_truncates_with_a_note(session, concept, monkeypatch):
    import fd_open_data_mcp.server as server

    monkeypatch.setattr(server, "MAX_SERIES_ROWS", 3)
    for year in range(2010, 2020):
        write_cache(session, concept, T, E, str(year), str(year), "USD", "worldbank")

    out = _call(concept, start="2009-01-01", end="2021-12-31")

    assert out["count"] == 3
    assert [p["date"] for p in out["points"]] == ["2017", "2018", "2019"]  # most recent kept
    assert "narrow the window" in out["note"]


def test_never_dispatches_upstream(session, concept, monkeypatch):
    """The whole point: a cache-only read must not reach the fetch layer."""
    import fd_open_data_mcp.fetch.dispatch as dispatch_mod

    def _boom(*args, **kwargs):
        raise AssertionError("read_series must never dispatch upstream")

    monkeypatch.setattr(dispatch_mod, "dispatch_one", _boom)
    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", _boom, raising=False)

    write_cache(session, concept, T, E, "2020", "2.0e12", "USD", "worldbank")
    out = _call(concept)
    assert out["count"] == 1


# ─── window bounds are parsed, never string-compared (audit 04-open-data #1) ──
#
# `date` is a text column holding the six shapes documented in
# semantic/dates.py. Lexicographically `"2023" < "2023-01-01"` (a bare year
# sorts BEFORE every day inside it) and `"2023-W20" > "2023-12-31"`
# (`'W' > '1'`), so a plain `date >= start AND date <= end` dropped bare-year,
# bare-month and ISO-week rows from every window — whole years missing, with no
# gap, no note and no error. Every average/CAGR over such a series was wrong.

SHAPE_DATES = ("2023", "2023-05", "2023-05-17", "2023-W20", "2026-W40", "LATEST")


def _write_shapes(session, concept_id):
    for d in SHAPE_DATES:
        write_cache(session, concept_id, T, E, d, f"v-{d}", "USD", "worldbank")


def test_window_returns_every_stored_date_shape_inside_it(session, concept):
    _write_shapes(session, concept)

    out = _call(concept, start="2023-01-01", end="2023-12-31")
    dates = [p["date"] for p in out["points"]]

    # Inside the window: the bare year, the bare month, the day, the ISO week
    # (2023-W20 is Monday 2023-05-15), ordered by the period each starts.
    assert dates == ["2023", "2023-05", "2023-W20", "2023-05-17"]
    # Outside it: an ISO week of another year is not in this window...
    assert "2026-W40" not in dates
    # ...and the first-crawl sentinel cannot sit in any window, so it is named
    # in `gaps` (same {start, end} shape as gap-fill) instead of vanishing.
    assert "LATEST" not in dates
    assert [g["start"] for g in out["gaps"]] == ["LATEST"]
    assert out["gaps"][0]["reason"] == "unparseable_date"
    assert "LATEST" in out["note"]


def test_window_is_a_period_intersection_not_a_string_range(session, concept):
    """A window inside the year still holds the annual row (that row answers
    every date in 2023 — the same rule the point reader applies), while a
    month/week that does not touch the window is excluded."""
    _write_shapes(session, concept)

    july = _call(concept, start="2023-07-01", end="2023-07-31")
    assert [p["date"] for p in july["points"]] == ["2023"]
    assert july.get("gaps") and july["gaps"][0]["start"] == "LATEST"

    may = _call(concept, start="2023-05-01", end="2023-05-31")
    assert [p["date"] for p in may["points"]] == ["2023", "2023-05", "2023-W20", "2023-05-17"]


def test_bare_year_bounds_mean_that_year(session, concept):
    _write_shapes(session, concept)

    out = _call(concept, start="2023", end="2023")

    assert [p["date"] for p in out["points"]] == ["2023", "2023-05", "2023-W20", "2023-05-17"]


def test_no_unparseable_row_means_no_gaps_key(session, concept):
    """The report is additive and only appears when there is something to say."""
    write_cache(session, concept, T, E, "2020", "2.0e12", "USD", "worldbank")

    out = _call(concept, start="2020", end="2020")

    assert "gaps" not in out and "note" not in out


# ─── fill_gaps: an explicit opt-in, never the default (design D3) ──────────


@pytest.fixture
def price_concept(session):
    c = Concept(code="price.close", entity_type="stock", frequency="daily",
                unit="currency", verified=True)
    session.add(c)
    session.commit()
    return c.id


def test_default_call_still_never_dispatches(session, price_concept) -> None:
    """The guarantee is now 'by default', and it must still hold exactly."""
    write_cache(session, price_concept, "stock", 7, "2026-03-02", "10.0",
                "currency", "akshare")
    out = _call(price_concept, entity_type="stock", entity_id=7,
                start="2026-01-01", end="2026-06-30")
    assert out["count"] == 1
    # The default path is the untouched cache-only read: no fill fields at all,
    # which is what makes "no surprise fetch" verifiable from the response.
    for field in ("fill_attempted", "fill_failed", "gaps", "fingerprint"):
        assert field not in out, f"a default read must not report {field}"


def test_default_call_on_a_partial_window_reports_no_fetch(session, price_concept) -> None:
    """A partial window without opt-in stays a coverage fact, not a fill."""
    write_cache(session, price_concept, "stock", 7, "2026-03-02", "10.0",
                "currency", "akshare")
    out = _call(price_concept, entity_type="stock", entity_id=7,
                start="2026-01-01", end="2026-06-30", fill_gaps=False)
    assert out["count"] == 1
    assert out.get("fill_attempted") in (None, False)


def test_fill_gaps_true_returns_the_fill_fields(session, price_concept) -> None:
    out = _call(price_concept, entity_type="stock", entity_id=7,
                start="2026-01-01", end="2026-01-31", fill_gaps=True)
    # No bindings exist in this fixture, so nothing can be fetched — but the
    # contract is that the fields are present and the failure is labelled
    # rather than raised.
    assert "gaps" in out
    assert "fill_attempted" in out
    assert "fill_failed" in out
    assert "fingerprint" in out
    assert isinstance(out["gaps"], list)


def test_fill_gaps_rejected_for_non_price_concept(session, concept) -> None:
    """Revised-value concepts must not be pinned by an additive fill."""
    out = _call(concept, fill_gaps=True)
    assert out["fill_attempted"] is False
    assert out.get("note")


def test_filling_twice_on_a_complete_window_is_stable(session, price_concept) -> None:
    """Fingerprint is only returned on the fill path; filling a complete window
    twice must yield the same content both times."""
    write_cache(session, price_concept, "stock", 7, "2026-01-05", "10.0",
                "currency", "akshare")
    args = dict(entity_type="stock", entity_id=7,
                start="2026-01-01", end="2026-01-10", fill_gaps=True)
    first = _call(price_concept, **args)
    second = _call(price_concept, **args)
    assert first["fingerprint"] == second["fingerprint"]
    assert first["points"] == second["points"]


# ─── a wrong entity LABEL is not a coverage gap (add-hotel-vertical, E16) ─────
#
# The star-rated quarterly survey stores one concept on country, province and
# city entities at once. The concept row declares a single ``entity_type``
# ('region'), while the national aggregate lands on a *country* entity — so a
# caller asking for (region, 1) used to read zero rows and be told "coverage
# gap", i.e. a type mismatch was disguised as missing data. The read now follows
# the landing class of the entity id it was given, and says so.

def test_read_by_landing_class_when_the_label_is_wrong(session):
    from fd_open_data_mcp.models import Entity

    c = Concept(code="hotel.cn.star_rated.occupancy.all.quarterly",
                entity_type="region", frequency="quarterly", unit="%",
                verified=True)
    session.add(c)
    session.commit()
    national = Entity(id=1, entity_type="country", code="CN", name_zh="中国")
    session.add(national)
    session.commit()
    for date, value in (("2023-06-30", "52.1"), ("2024-09-30", "52.96")):
        write_cache(session, c.id, "country", 1, date, value, "%", "mct-star-hotel")

    out = _call(c.id, entity_type="region", entity_id=1,
                start="2023-01-01", end="2024-12-31")

    assert out["count"] == 2, out
    assert out["points"][-1]["value"] == pytest.approx(52.96)
    assert out["landed_entity_type"] == "country"
    assert "read by landing" in out["note"]
    assert "coverage gap" not in out["note"]


def test_a_genuinely_empty_window_is_still_a_coverage_gap(session, concept):
    """The fallback must not relabel an empty window as a type problem."""
    out = _call(concept)  # concept declares country, entity 42 is absent anyway
    assert out["count"] == 0
    assert "coverage gap" in out["note"]
    assert "landed_entity_type" not in out


def test_year_granularity_series_is_judged_on_its_own_cadence(session, concept):
    """按年的序列，覆盖完整时必须判 complete。

    2026-11-11 实测的缺陷：coverage() 把日期串一律当"某一天"喂给 _width/_prev_day，
    按年的 `"2020"` 第一个就抛 ValueError（`split("-")` 拆不出三段），异常被上层
    吞成 "coverage unknown" —— 每一读都降级，而本项目多数宏观数据正是按年的。
    """
    for year in ("2019", "2020", "2021"):
        write_cache(session, concept, T, E, year, "1.0", "USD", "worldbank")

    out = _call(concept, start="2019", end="2021")

    assert out["coverage"]["complete"] is True
    assert out["coverage"]["missing"] == []
    assert "note" not in out


def test_year_series_partial_window_is_noted(session, concept):
    """窗口超出实际覆盖范围时，必须如实报缺——不能因为按年就默认完整。"""
    for year in ("2019", "2020", "2021"):
        write_cache(session, concept, T, E, year, "1.0", "USD", "worldbank")

    out = _call(concept, start="2018-01-01", end="2022-12-31")

    assert out["coverage"]["complete"] is False
    assert out["coverage"]["missing"] == [["2018-01-01", "2018-12-31"],
                                          ["2022-01-01", "2022-12-31"]]
    assert "incomplete" in out["note"]

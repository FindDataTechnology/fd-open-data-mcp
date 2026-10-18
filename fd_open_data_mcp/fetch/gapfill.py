"""On-demand gap filling for price series (design D1/D2/D6, capability
``price-on-demand-fill``).

This is the orchestration layer over pieces that already existed:

- :func:`fetch.coverage.coverage` decides *what* is missing;
- ``dispatch._fetch_series_from`` does the actual upstream range call;
- :func:`fetch.cache.write_cache_range_new_only` adds only what is absent.

What it adds is the part nobody had: **a partially covered window used to come
back as if it were complete.** ``dispatch._read_range_one`` judges coverage by
staleness alone, so a window holding twelve cached days inside a two-year
request returned those twelve days with no indication that anything was absent.
For a price series that is silently wrong data — a backtest would compute a
return over twelve days and present it as a two-year result.

The fill is deliberately **additive**: only the missing intervals are fetched,
and a stored value is never overwritten. Both halves matter — fetching the whole
window would make cost proportional to the window instead of to the gap, and
overwriting would let today's forward-adjusted series rewrite the history an
existing run was computed from.

Failure is a labelled result, never an exception: a caller that asked for a fill
gets whatever is held plus ``gaps`` / ``fill_failed``, because partial data with
a stated gap is decidable while an exception loses the partial data entirely.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Callable, Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.fetch import dispatch as _dispatch
from fd_open_data_mcp.fetch.cache import (
    _read_base_range,
    read_cache_range,
    write_cache_range_new_only,
)
from fd_open_data_mcp.fetch.coverage import coverage
from fd_open_data_mcp.models import Concept

logger = logging.getLogger(__name__)

#: Concepts eligible for gap filling. Price series are the motivating case: they
#: are dense, historical, and fetched by range. Filling a concept whose values
#: are revised (GDP revisions, restated financials) would pin an old value in
#: place forever, so the set is explicit rather than "anything".
FILLABLE_PREFIXES = ("price.",)


def is_fillable(concept: Concept | None) -> bool:
    """Whether this concept's gaps may be filled on demand."""
    if concept is None:
        return False
    return any(concept.code.startswith(p) for p in FILLABLE_PREFIXES)


def series_dates(
    session: Session, concept_id: int, entity_type: str, entity_id: int
) -> list[str]:
    """Every date this series has a point on (any source), date-ordered.

    This is the series' own trading calendar — the basis for judging coverage
    without an external calendar (design D1).
    """
    rows = _read_base_range(
        session, concept_id, entity_type, entity_id, "0000-01-01", "9999-12-31"
    )
    return sorted({r.date for r in rows})


def fingerprint(dates_values: list[tuple[str, object]]) -> str:
    """Content hash of one returned series (design D4).

    Derived from the (date, value) pairs actually returned, so it changes when
    the data changes and stays stable across identical reads. Callers record it
    to pin "the data this run saw"; it is deliberately not stored here, because
    the same stored rows yield different fingerprints under different windows.
    """
    h = hashlib.sha256()
    for date, value in sorted(dates_values):
        h.update(f"{date}={value}\n".encode())
    return h.hexdigest()


def fill_gaps(
    session: Session,
    concept_id: int,
    entity_type: str,
    entity_id: int,
    start: str,
    end: str,
    *,
    fetch_range: Optional[Callable[[str, str], dict]] = None,
    source: Optional[str] = None,
) -> dict:
    """Assess coverage, fetch the missing intervals, return the merged series.

    ``fetch_range(start, end) -> {date: value}`` performs one upstream range
    read; it defaults to the real dispatch path. Injecting it is what lets tests
    assert *which* intervals were requested — the property that separates "fills
    the gap" from "re-fetches everything".

    Returns a dict with ``points``, ``gaps`` (intervals still missing),
    ``fill_attempted``, ``fill_failed``, and ``fingerprint``.
    """
    concept = session.get(Concept, concept_id)
    if not is_fillable(concept):
        return {
            "points": [], "gaps": ((start, end),), "missing": [{"start": start, "end": end}],
            "market_closed": [], "fill_attempted": False,
            "fill_failed": False, "fingerprint": fingerprint([]),
            "note": "concept is not eligible for gap filling",
        }
    assert concept is not None

    dates = series_dates(session, concept_id, entity_type, entity_id)
    probed = _known_empty(session, concept_id, entity_type, entity_id)
    report = coverage(dates, start, end, known_empty=probed)
    # A cold series has no probed-empty record and its whole window is a gap, so
    # it must still be fetched: "nothing fetchable" here means either "complete"
    # or "everything left is already known to be absent".
    if report.complete or (not report.fetchable and report.gaps):
        merged = _read(session, concept_id, entity_type, entity_id, start, end)
        return _result(merged, report.gaps, False, False,
                       known_empty=report.known_empty)

    resolver = fetch_range or (
        lambda a, b: _fetch_interval(
            session, concept, entity_type, entity_id, a, b, source
        )
    )
    failed = False
    attempted = False
    # Only intervals not already known to be empty. A market holiday looks
    # exactly like a hole to the coverage judgment, but asking again is a wasted
    # round trip: measured six per read on a two-year A-share window.
    for gap_start, gap_end in report.fetchable:
        attempted = True
        try:
            series, used_source = resolver(gap_start, gap_end)
        except Exception as exc:  # noqa: BLE001 - one bad interval must not kill the rest
            logger.warning(
                "gap fill failed for concept %s entity %s/%s [%s..%s]: %s",
                concept_id, entity_type, entity_id, gap_start, gap_end, exc,
            )
            failed = True
            continue
        if not series:
            # Distinguish "asked and there is nothing" from "could not ask".
            # A source that answered with an empty series proves the interval is
            # genuinely unpopulated (a market holiday) and is worth remembering;
            # a failure proves nothing and must stay retryable — conflating the
            # two would silently stop retrying a transient outage.
            if used_source:
                _record_empty(session, concept_id, entity_type, entity_id,
                              gap_start, gap_end)
            else:
                failed = True
            continue
        # Additive only: a stored value survives even if upstream now differs.
        write_cache_range_new_only(
            session, concept_id, entity_type, entity_id, series,
            concept.unit, used_source,
        )

    # Re-assess rather than assume: a fill that partially succeeded leaves real
    # gaps, and reporting the ones that remain is the whole point.
    after = coverage(
        series_dates(session, concept_id, entity_type, entity_id), start, end,
        known_empty=_known_empty(session, concept_id, entity_type, entity_id),
    )
    merged = _read(session, concept_id, entity_type, entity_id, start, end)
    # Report every remaining gap: known-empty ones (the series does not trade
    # there) and unfetched ones alike, so a caller can always tell complete from
    # partial. Known-empty intervals are not a *failure*, which fill_failed says.
    return _result(merged, after.gaps, attempted, failed,
                   known_empty=after.known_empty)


def _known_empty(
    session: Session, concept_id: int, entity_type: str, entity_id: int
) -> list[tuple[str, str]]:
    from fd_open_data_mcp.models import PriceEmptyInterval

    rows = (
        session.query(PriceEmptyInterval)
        .filter_by(concept_id=concept_id, entity_type=entity_type,
                   entity_id=entity_id)
        .all()
    )
    return [(r.start, r.end) for r in rows]


def _record_empty(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from fd_open_data_mcp.models import PriceEmptyInterval

    session.add(PriceEmptyInterval(
        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
        start=start, end=end,
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # already recorded by a concurrent fill


def _fetch_interval(
    session: Session, concept: Concept, entity_type: str, entity_id: int,
    start: str, end: str, source: Optional[str] = None,
) -> tuple[dict, str]:
    """One upstream range call for one missing interval.

    Deliberately the same bricks ``dispatch._read_range_one`` uses — ranked
    sources, ``_build_range_params``, ``_extract_series`` — so a gap fill and a
    first fetch agree on what a range fetch means. It returns the source that
    answered as well as the series, because the rows must be written under that
    source's name: writing under a guessed source would create a second,
    parallel history for the same concept.

    The returned source is what tells the caller which of two empty outcomes
    happened, and the distinction is load-bearing:

    * ``({}, "")`` — no source could be asked (all threw, or none resolved an
      identifier). Proven nothing; the interval must stay retryable.
    * ``({}, "<source>")`` — a source answered and its series was empty. That is
      positive evidence the interval holds no data (a market holiday), which is
      what makes it recordable as ``known_empty`` and keeps the window from
      being re-probed on every read.

    Collapsing the second case into the first would make ``_record_empty`` dead
    code and leave every holiday indistinguishable from a hole — the read path
    used to do exactly that.
    """
    asked: Optional[str] = None
    for cand in _dispatch.rank_sources_for_concept(session, concept.id, None):
        cand_source = cand["source"]
        if source is not None and cand_source != source:
            continue
        identifier = _dispatch.resolve_identifier(
            session, entity_type, entity_id, cand_source
        )
        if identifier is None:
            continue
        for binding, fn in _dispatch._bindings_for_source(
            session, concept.id, cand_source
        ):
            params = _dispatch._build_range_params(fn, identifier, start, end, binding)
            try:
                result = _dispatch.instrumented_fetch(
                    cand_source, fn.command, params,
                    session=session, function_id=fn.id,
                    concept_id=concept.id, entity_type=entity_type,
                    entity_id=entity_id,
                )
            except Exception as exc:  # noqa: BLE001 - ranked failover: try the next
                logger.warning(
                    "gap fill source %s failed for [%s..%s]: %s",
                    cand_source, start, end, exc,
                )
                continue
            # The source answered (it did not throw): from here on an empty
            # series is evidence of absence, not of failure.
            asked = cand_source
            series = _dispatch._extract_series(
                result, binding.column.name, start, end, cand_source, fn.command,
            )
            if series:
                return series, cand_source
    return {}, (asked or "")


def _read(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str,
) -> list[dict]:
    rows = read_cache_range(session, concept_id, entity_type, entity_id, start, end)
    return [
        {"date": r.date, "value": _dispatch._coerce_value(r.value),
         "unit": r.unit, "source_used": r.source_used}
        for r in rows
    ]


def _result(
    points: list[dict], gaps, attempted: bool, failed: bool,
    known_empty=(),
) -> dict:
    """Build the response, keeping the two kinds of absence distinguishable.

    ``gaps`` mixes two very different facts and a consumer cannot act correctly
    on the blend:

    * **market closed** — asked upstream and there is genuinely no data (a
      holiday). The series is complete for every day it trades.
    * **missing** — we do not have it and did not prove it absent. The series is
      partial, and any statistic computed over it is suspect.

    A backtest must reject the second and accept the first; reporting them as one
    list would force the consumer to either re-derive our trading calendar (a
    second implementation of the rules, which is exactly what this capability
    exists to prevent) or reject every window containing a holiday.
    """
    known = set(known_empty)
    pairs = [(p["date"], p["value"]) for p in points]
    return {
        "points": points,
        "gaps": [{"start": a, "end": b} for a, b in gaps],
        # The actionable subset: intervals not proven to be non-trading.
        "missing": [
            {"start": a, "end": b} for a, b in gaps if (a, b) not in known
        ],
        # Intervals proven to hold nothing upstream (market closed).
        "market_closed": [
            {"start": a, "end": b} for a, b in gaps if (a, b) in known
        ],
        "fill_attempted": attempted,
        "fill_failed": failed,
        "fingerprint": fingerprint(pairs),
    }

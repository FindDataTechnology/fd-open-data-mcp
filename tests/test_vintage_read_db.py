"""as-of 读的端到端行为（对应 make-observation-vintage-readable 任务 4.5）。

要守住的两条：
1. 不传 ``as_of_vintage`` 时行为与改动前完全一致（零影响）。
2. 传了之后，不可回放的点**不返回数值**，并在 ``vintage`` 里自证。
"""

from datetime import datetime, timedelta, timezone

import pytest

from fd_open_data_mcp.fetch.vintage import read_range_as_of
from fd_open_data_mcp.models import Concept, ObservationRevision, SemanticObservation


@pytest.fixture
def priced(session):
    concept = Concept(code="probe.vintage", entity_type="country",
                      frequency="yearly", unit="%", verified=True)
    session.add(concept)
    session.flush()
    return concept


def seed(session, concept, date, value, *, fetched_at=None, granularity="year"):
    row = SemanticObservation(
        concept_id=concept.id, entity_type="country", entity_id=1, date=date,
        granularity=granularity, value=value, unit="%", source_used="probe",
        fetched_at=fetched_at or datetime(2024, 1, 1),
    )
    session.add(row)
    session.commit()
    return row


def add_revision(session, concept, date, old, new, revised_at, *, granularity="year"):
    rev = ObservationRevision(
        concept_id=concept.id, entity_type="country", entity_id=1, date=date,
        granularity=granularity, source_used="probe", revision_kind="overwrite",
        old_value=old, new_value=new, revised_at=revised_at,
    )
    session.add(rev)
    session.commit()
    return rev


def test_exact_replay_through_db(session, priced):
    seed(session, priced, "2023-12-31", "v3")
    add_revision(session, priced, "2023-12-31", "v1", "v2", datetime(2024, 6, 1))
    add_revision(session, priced, "2023-12-31", "v2", "v3", datetime(2024, 9, 1))

    mid = read_range_as_of(session, priced.id, "country", 1,
                           "2023-01-01", "2023-12-31", "2024-07-01")

    assert mid["count"] == 1
    assert mid["values"][0]["value"] == "v2"
    assert mid["vintage"]["fully_exact"] is True


def test_batch_span_omits_value_and_self_reports(session, priced):
    seed(session, priced, "2021-12-31", "999")
    session.add(ObservationRevision(
        concept_id=priced.id, entity_type="country", entity_id=1, date="2020-01-01",
        granularity="year", source_used="probe", revision_kind="batch",
        batch_id="adj-1", range_start="2020-01-01", range_end="2022-12-31",
        revised_at=datetime(2024, 5, 1),
    ))
    session.commit()

    out = read_range_as_of(session, priced.id, "country", 1,
                           "2020-01-01", "2023-12-31", "2024-06-01")

    assert out["count"] == 0, "不可回放的点不得返回数值"
    assert out["vintage"]["batch_unreplayable"] == 1
    assert out["vintage"]["fully_exact"] is False
    assert out["details"][0]["batch_id"] == "adj-1"


def test_point_absent_at_requested_instant(session, priced):
    seed(session, priced, "2020-12-31", "5.2", fetched_at=datetime(2022, 3, 1))

    out = read_range_as_of(session, priced.id, "country", 1,
                           "2020-01-01", "2020-12-31", "2021-01-01")

    assert out["count"] == 0
    assert out["vintage"]["absent"] == 1


def test_no_as_of_returns_current_values(session, priced):
    seed(session, priced, "2020-12-31", "5.2")

    out = read_range_as_of(session, priced.id, "country", 1,
                           "2020-01-01", "2020-12-31", None)

    assert out["count"] == 1
    assert out["values"][0]["value"] == "5.2"


def test_granularity_mismatch_is_unavailable_not_absent(session, priced):
    """观测写成 day、修订记在 year 上：键对不上，此时**不许**判 absent。

    `absent` 是断言「当时还没这条数据」；粒度错配时我们并不知道当时有没有，
    正确答案是 `vintage_unavailable`（无法判定）。这条守卫防止把不确定说成确定。
    """
    seed(session, priced, "2020-12-31", "5.4", granularity="day",
         fetched_at=datetime(2026, 11, 1))
    add_revision(session, priced, "2020-12-31", "5.2", "5.4",
                 datetime(2024, 6, 1), granularity="year")

    out = read_range_as_of(session, priced.id, "country", 1,
                           "2020-01-01", "2020-12-31", "2025-01-01")

    assert out["count"] == 0
    assert out["vintage"]["vintage_unavailable"] == 1
    assert out["vintage"]["absent"] == 0, "不得把粒度错配报成 absent"
    assert "粒度" in out["details"][0]["detail"]

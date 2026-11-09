"""定型写闸（对应 make-observation-vintage-readable 任务 3.1–3.4）。

要守住的边界：闸拦的是「**改写**已有已定型行」，不是「写入历史点」。
把首次写入也挡掉，历史区间就永远补不了数——那正是 `test_read_range.py` 冷区间
用例在守的行为。
"""

from datetime import datetime, timedelta, timezone

import pytest

from fd_open_data_mcp.errors import FinalizedObservationError
from fd_open_data_mcp.fetch.cache import write_cache, write_cache_range
from fd_open_data_mcp.models import Concept, ObservationRevision, SemanticObservation


@pytest.fixture
def concept(session):
    c = Concept(code="probe.gate", entity_type="country",
                frequency="yearly", unit="%", verified=True)
    session.add(c)
    session.flush()
    return c


def _count(session):
    return session.query(SemanticObservation).count()


def _revisions(session):
    return session.query(ObservationRevision).count()


# --- 3.1 定型后改写被拒 -----------------------------------------------------

def test_rewriting_finalized_point_is_refused_with_coordinates(session, concept):
    write_cache(session, concept.id, "country", 1, "2020-12-31", "5.2", "%",
                "probe", granularity="year")

    with pytest.raises(FinalizedObservationError) as exc:
        write_cache(session, concept.id, "country", 1, "2020-12-31", "9.9", "%",
                    "probe", granularity="year")

    assert exc.value.coordinates, "拒绝必须指名坐标"
    assert exc.value.coordinates[0][3] == "2020-12-31"
    # 既有值未变
    row = session.query(SemanticObservation).one()
    session.refresh(row)
    assert row.value == "5.2"


def test_unfinalized_point_still_overwrites_with_revision_trail(session, concept):
    """当期（未定型）照旧可覆写，且旧值必须留痕。"""
    today = datetime.now(timezone.utc).date().isoformat()
    write_cache(session, concept.id, "country", 1, today, "1.0", "%", "probe",
                granularity="day")
    write_cache(session, concept.id, "country", 1, today, "1.5", "%", "probe",
                granularity="day")

    row = session.query(SemanticObservation).one()
    session.refresh(row)
    assert row.value == "1.5"


def test_first_write_of_historical_point_is_allowed(session, concept):
    """补数：库里没有这个历史点 → 允许写入（不是修订）。"""
    write_cache(session, concept.id, "country", 1, "2015-12-31", "4.4", "%",
                "probe", granularity="year")

    assert _count(session) == 1


# --- 3.2 correction 通道 ----------------------------------------------------

def test_correction_requires_reason(session, concept):
    write_cache(session, concept.id, "country", 1, "2020-12-31", "5.2", "%",
                "probe", granularity="year")

    with pytest.raises(FinalizedObservationError):
        write_cache(session, concept.id, "country", 1, "2020-12-31", "5.4", "%",
                    "probe", granularity="year", correction=True, reason="   ")


def test_correction_with_reason_is_allowed_and_leaves_trail(session, concept):
    write_cache(session, concept.id, "country", 1, "2020-12-31", "5.2", "%",
                "probe", granularity="year")

    write_cache(session, concept.id, "country", 1, "2020-12-31", "5.4", "%",
                "probe", granularity="year", correction=True,
                reason="官方年鉴勘误：2020 年值由 5.2 修正为 5.4")

    row = session.query(SemanticObservation).one()
    session.refresh(row)
    assert row.value == "5.4"
    rev = session.query(ObservationRevision).one()
    assert rev.old_value == "5.2" and rev.new_value == "5.4"
    assert "勘误" in (rev.reason or "")


# --- 区间写入口 -------------------------------------------------------------

def test_range_write_refuses_mixed_window_without_reason(session, concept):
    write_cache(session, concept.id, "country", 1, "2020-12-31", "5.2", "%",
                "probe", granularity="year")

    with pytest.raises(FinalizedObservationError):
        write_cache_range(session, concept.id, "country", 1,
                          {"2020-12-31": "6.0", "2021-12-31": "6.1"},
                          "%", "probe", granularity="year")

    # 整批未落：定型行与新增行都不该写进去（fail-closed，不留半截）
    dates = {r.date for r in session.query(SemanticObservation).all()}
    assert dates == {"2020-12-31"}


def test_range_write_allows_new_dates_alongside_finalized_rows(session, concept):
    """只含新日期的区间（补数）不受闸影响。"""
    n = write_cache_range(session, concept.id, "country", 1,
                          {"2018-12-31": "4.4", "2019-12-31": "4.9"},
                          "%", "probe", granularity="year")

    assert n == 2


def test_range_write_with_reason_corrects_and_trails(session, concept):
    write_cache(session, concept.id, "country", 1, "2020-12-31", "5.2", "%",
                "probe", granularity="year")

    write_cache_range(session, concept.id, "country", 1, {"2020-12-31": "5.4"},
                      "%", "probe", granularity="year",
                      revision_reason="官方勘误")

    row = session.query(SemanticObservation).one()
    session.refresh(row)
    assert row.value == "5.4"
    assert _revisions(session) == 1


# --- 3.4 声明非法时 fail-closed ---------------------------------------------

def test_illegal_window_declaration_fails_closed(session, concept):
    """概念上的窗口声明非法 → 抛错，不静默退化成"全部定型"或"全部不定型"。"""
    concept.finalize_grace_days = -3
    session.commit()
    write_cache(session, concept.id, "country", 1, "2020-12-31", "5.2", "%",
                "probe", granularity="year")

    with pytest.raises(ValueError):
        write_cache(session, concept.id, "country", 1, "2020-12-31", "5.3", "%",
                    "probe", granularity="year")

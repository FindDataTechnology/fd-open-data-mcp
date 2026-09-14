"""Coverage freshness heatmap tests (panel-ui-refresh Phase 3).

Spec scenario: concepts observed 2/12/45 days ago plus one with no
observations render one tile per bucket with counts 1,1,1,1 and a legend
naming thresholds; selecting a bucket filters the detail table.
"""
from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import Concept, SemanticObservation
from fd_open_data_mcp.panel.app import app

client = TestClient(app)


def _seed():
    today = dt.date.today()
    s = get_database().get_session()
    try:
        defs = [("gdp", today - dt.timedelta(days=2)),   # fresh
                ("cpi", today - dt.timedelta(days=12)),  # aging
                ("unemp", today - dt.timedelta(days=45)),  # stale
                ("never", None)]                          # never observed
        for code, latest in defs:
            c = Concept(code=code, entity_type="country", name_zh=code,
                        category="macro", frequency="monthly")
            s.add(c)
            s.commit()
            s.refresh(c)
            if latest:
                s.add(SemanticObservation(
                    concept_id=c.id, entity_type="country", entity_id=1,
                    date=latest.isoformat(), value="1", source_used="wbgapi"))
                s.commit()
    finally:
        s.close()


def test_heatmap_buckets_counts_and_legend(session):
    _seed()
    r = client.get("/panel/data")
    assert r.status_code == 200
    text = r.text
    # one tile per bucket with count 1 (spec scenario: 2d/12d/45d/never)
    for key in ("fresh", "aging", "stale", "never"):
        assert f'class="heat-{key}"' in text, key
    # titles carry bucket label + threshold + count (no-JS inspectable)
    for title in ("新鲜 ≤7d (≤ 7d): 1", "过期 &gt;30d (&gt; 30d): 1"):
        assert title in text, title
    assert "新鲜" in text and "≤ 7d" in text  # legend names thresholds
    # tiles drill into the filtered table
    assert 'href="/panel/data?freshness=stale"' in text


def test_bucket_selection_filters_table(session):
    _seed()
    r = client.get("/panel/data", params={"freshness": "stale"})
    text = r.text
    assert "unemp" in text          # stale concept stays
    assert ">gdp<" not in text and ">cpi<" not in text
    assert "heat-active" in text    # selected bucket outlined
    assert "清除新鲜度筛选" in text

    # never-observed concepts have no coverage rows -> empty table state
    never = client.get("/panel/data", params={"freshness": "never"}).text
    assert "没有匹配的观测 No observations match" in never

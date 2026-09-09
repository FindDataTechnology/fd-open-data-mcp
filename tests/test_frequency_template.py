"""Frequency-template tests (panel-ops-console, spec crawl-control-center).

Covers: the daily template pre-selects verified non-deprecated daily concepts
and proposes a nightly trailing-1d schedule; saving produces an ordinary
policy editable by the ordinary editor; template hygiene excludes
deprecated/unverified concepts.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from fd_open_data_mcp.models import Concept, CrawlPolicy
from fd_open_data_mcp.panel.app import _template_concepts, app

client = TestClient(app)


def _concept(session, code, frequency, deprecated=False, verified=True) -> Concept:
    c = Concept(code=code, name_en=code, entity_type="stock", frequency=frequency,
                deprecated=deprecated, verified=verified)
    session.add(c)
    session.commit()
    return c


def test_template_preselects_and_proposes_schedule(session):
    import re
    daily = _concept(session, "price.close", "daily")
    _concept(session, "index.monthly", "monthly")  # different cadence: excluded
    r = client.get("/panel/policies/template", params={"frequency": "daily"})
    assert r.status_code == 200
    # pre-selection: the daily concept is checked, the monthly one is not
    assert re.search(rf'value="{daily.id}"\s+checked', r.text)
    assert not re.search(r'value="\d+"\s+checked.*index\.monthly', r.text, re.S)
    # proposals: nightly cron + trailing-1d date policy
    assert 'name="cron_expr" value="0 6 * * *"' in r.text
    assert 'name="date_policy_days" value="1"' in r.text
    # it is a template, not an edit of an existing row: no policy_id hidden value
    assert 'name="policy_id" value=""' in r.text or 'name="policy_id">' in r.text


def test_template_save_produces_ordinary_policy(session):
    daily = _concept(session, "price.close", "daily")
    r = client.get("/panel/policies/template", params={"frequency": "daily"})
    assert r.status_code == 200
    # simulate submitting the template form (name filled by the operator)
    client.post("/panel/policies/save", data={
        "name": "nightly-daily", "entity_type": "fund",
        "concept_ids": [str(daily.id)], "frequency": "daily", "mode": "per_date",
        "date_policy_mode": "trailing", "date_policy_days": "1",
        "cron_expr": "0 6 * * *", "timezone": "UTC", "enabled": "on"})
    p = session.query(CrawlPolicy).filter_by(name="nightly-daily").one()
    assert p.concept_ids == [daily.id]
    assert p.date_policy == {"mode": "trailing", "days": 1}
    assert p.cron_expr == "0 6 * * *"
    # the saved policy edits through the ordinary editor without artifacts
    r = client.get(f"/panel/policies/{p.id}")
    assert r.status_code == 200 and 'value="nightly-daily"' in r.text


def test_template_hygiene_excludes_deprecated_and_unverified(session):
    _concept(session, "a.ok", "daily")
    _concept(session, "b.deprecated", "daily", deprecated=True)
    _concept(session, "c.unverified", "daily", verified=False)
    codes = [c.code for c in _template_concepts(session, "daily")]
    assert codes == ["a.ok"]


def test_template_weekly_props_since_last(session):
    _concept(session, "w.ind", "weekly")
    r = client.get("/panel/policies/template", params={"frequency": "weekly"})
    assert r.status_code == 200
    assert 'value="0 6 * * 1"' in r.text  # weekly cron proposal
    # non-daily cadences propose since_last (a trailing-7d window would refetch)
    assert "since_last" in r.text

"""Panel source-discovery funnel tests (harness-platform-integration 2.1-2.3).

Covers: the funnel stage counts over the mirrored pipeline tables
(discoveries/candidates/analyses/manifests incl. landed = crawl_sources
match), the latest pending-approval queue, the landed marker on approved
manifests, the source-detail backlink (来自发现流水线), and the standard
partial degradation. All data is seeded directly through the models on the
per-test sqlite fixture — the panel never writes the pipeline tables.
"""
from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import (
    Analysis, Candidate, CrawlSite, CrawlSource, Discovery, SourceManifest,
)
from fd_open_data_mcp.panel.app import app

client = TestClient(app)
HX = {"HX-Request": "true"}

NOW = dt.datetime.utcnow()
BASE = NOW - dt.timedelta(days=30)


# ── seed helpers ────────────────────────────────────────────────────────────
def _site(sid="tencent") -> str:
    s = get_database().get_session()
    try:
        if s.get(CrawlSite, sid) is None:
            s.add(CrawlSite(id=sid, description="main site", kind="k8s",
                            enabled=True))
            s.commit()
        return sid
    finally:
        s.close()


def _src(name, site="tencent", schedule="0 6 * * *") -> str:
    _site(site)
    s = get_database().get_session()
    try:
        if s.get(CrawlSource, name) is None:
            s.add(CrawlSource(source=name, site=site, schedule=schedule,
                              enabled=True, last_commit="abc123"))
            s.commit()
        return name
    finally:
        s.close()


def _disc(query="新能源数据", status="done") -> int:
    s = get_database().get_session()
    try:
        d = Discovery(query=query, query_type="topic", status=status,
                      created_at=BASE, updated_at=BASE)
        s.add(d)
        s.commit()
        return d.id
    finally:
        s.close()


def _cand(discovery_id, url="https://example.org/data") -> int:
    s = get_database().get_session()
    try:
        c = Candidate(discovery_id=discovery_id, url=url, title="example",
                      score=80, coverage_status="not_covered")
        s.add(c)
        s.commit()
        return c.id
    finally:
        s.close()


def _ana(candidate_id, discovery_id) -> int:
    s = get_database().get_session()
    try:
        a = Analysis(candidate_id=candidate_id, discovery_id=discovery_id,
                     page_url="https://example.org/data", status="done")
        s.add(a)
        s.commit()
        return a.id
    finally:
        s.close()


def _man(source_name, status="draft", *, updated=None, model="glm-test",
         discovery_id=None, analysis_id=None) -> int:
    s = get_database().get_session()
    try:
        m = SourceManifest(discovery_id=discovery_id, analysis_id=analysis_id,
                           manifest_yaml="source: x", source_name=source_name,
                           model_used=model, status=status,
                           created_at=BASE,
                           updated_at=updated or NOW)
        s.add(m)
        s.commit()
        return m.id
    finally:
        s.close()


def _funnel() -> dict:
    from fd_open_data_mcp.visibility import snapshot

    s = get_database().get_session()
    try:
        return snapshot.discovery_funnel(s)
    finally:
        s.close()


# ── 2.1/2.2 stage counts ─────────────────────────────────────────────────────
def test_funnel_counts_across_stages(session):
    d1, d2 = _disc("q1"), _disc("q2")
    c1, c2, c3 = _cand(d1), _cand(d1), _cand(d2)        # 3 candidates
    _ana(c3, d2)                                        # 1 analysis
    _src("landed-src")                                  # crawl_sources match
    _man("landed-src", "approved", discovery_id=d1)     # approved + landed
    _man("not-landed-src", "approved", discovery_id=d1)  # approved, no match
    _man("draft-a", "draft", discovery_id=d2)           # pending
    _man("draft-b", "draft", discovery_id=d2)
    _man("rejected-x", "rejected", discovery_id=d2)     # neither pending nor approved

    counts = _funnel()["counts"]
    assert counts == {"discoveries": 2, "candidates": 3, "analyses": 1,
                      "generated": 5, "pending": 2, "approved": 2,
                      "landed": 1}

    # the same numbers surface through the polled partial
    r = client.get("/panel/partials/funnel")
    assert r.status_code == 200 and "<html" not in r.text
    for stage, label in (("discoveries", "发现"), ("candidates", "候选"),
                         ("analyses", "已分析"), ("generated", "已生成"),
                         ("pending", "待批准"), ("approved", "已批准"),
                         ("landed", "已落地")):
        assert label in r.text
        assert f">{counts[stage]}<" in r.text


def test_funnel_empty_database_renders_zeros(session):
    r = client.get("/panel/partials/funnel")
    assert r.status_code == 200 and "<html" not in r.text
    assert ">0<" in r.text and "队列为空" in r.text  # empty queue copy
    assert _funnel() == {
        "counts": {"discoveries": 0, "candidates": 0, "analyses": 0,
                   "generated": 0, "pending": 0, "approved": 0, "landed": 0},
        "pending": [], "approved": []}


# ── 2.2 page shell, polling, read-only note, degradation ────────────────────
def test_funnel_page_shell_polling_and_readonly_note(session):
    page = client.get("/panel/funnel").text
    assert "候选源漏斗" in page and "Discovery funnel" in page
    # the shell carries the polled region; the page itself never queries
    assert 'hx-get="/panel/partials/funnel"' in page
    assert "every 15s" in page
    # approval is tool-surface only — stated bilingually
    assert "审批在 harness 工具面完成（agent 操作），此处只读" in page
    assert "this view is read-only" in page
    # nav entry from the home cockpit
    assert 'href="/panel/funnel"' in client.get("/panel").text


def test_funnel_partial_degrades_like_every_home_partial(session):
    from unittest.mock import patch

    from fd_open_data_mcp.visibility import snapshot

    with patch.object(snapshot, "discovery_funnel",
                      side_effect=RuntimeError("pipeline db gone")):
        down = client.get("/panel/partials/funnel")
    assert down.status_code == 200
    assert "section unavailable" in down.text
    assert "pipeline db gone" in down.text


def test_funnel_routes_behind_token_gate(session, monkeypatch):
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    from importlib import reload

    import fd_open_data_mcp.panel.app as appmod

    gated = TestClient(reload(appmod).app)
    assert gated.get("/panel/funnel").status_code == 401
    assert gated.get("/panel/partials/funnel").status_code == 401
    ok = {"X-Panel-Token": "sekret"}
    assert gated.get("/panel/funnel", headers=ok).status_code == 200
    assert gated.get("/panel/partials/funnel", headers=ok).status_code == 200
    monkeypatch.undo()
    reload(appmod)


# ── 2.2 latest pending-approval queue ────────────────────────────────────────
def test_funnel_pending_queue_latest_first_limited(session):
    d = _disc("queue")
    # 12 drafts, updated_at strictly increasing: draft-12 is the newest
    for i in range(1, 13):
        _man(f"draft-{i:02d}", "draft", discovery_id=d,
             updated=BASE + dt.timedelta(hours=i), model=f"model-{i:02d}")
    _man("old-approved", "approved", discovery_id=d,
         updated=BASE - dt.timedelta(days=1))  # never in the queue

    f = _funnel()
    assert f["counts"]["pending"] == 12
    names = [m["source_name"] for m in f["pending"]]
    assert len(names) == 10                       # limited to 10
    assert names == [f"draft-{i:02d}" for i in range(12, 2, -1)]  # DESC
    assert "draft-01" not in names and "draft-02" not in names
    assert "old-approved" not in names
    head = f["pending"][0]
    assert head["source_name"] == "draft-12"
    assert head["model_used"] == "model-12"
    assert head["updated_at"] == (BASE + dt.timedelta(hours=12)).isoformat()

    # the partial surfaces the queue rows with model + timestamp
    r = client.get("/panel/partials/funnel")
    assert "draft-12" in r.text and "model-12" in r.text
    assert "draft-01" not in r.text and "draft-02" not in r.text  # cut off
    # the approved manifest stays out of the QUEUE region (it renders in the
    # approved list below instead)
    queue_html = r.text.split("最新待批准", 1)[1].split("「已落地」", 1)[0]
    assert "old-approved" not in queue_html
    assert "draft-12" in queue_html and "draft-03" in queue_html


# ── 2.3 landed marker (approved + crawl_sources match) ──────────────────────
def test_funnel_landed_marker_requires_approved_and_source_match(session):
    d = _disc("landed")
    _src("seed-src")                                # in crawl_sources
    _man("seed-src", "approved", discovery_id=d)    # approved + match -> landed
    _man("pending-seed", "draft", discovery_id=d)   # draft never lands
    m_unmatched = _man("ghost-src", "approved", discovery_id=d)  # no crawl row

    f = _funnel()
    assert f["counts"]["landed"] == 1 and f["counts"]["approved"] == 2
    by_name = {m["source_name"]: m for m in f["approved"]}
    assert by_name["seed-src"]["landed"] is True
    assert by_name["ghost-src"]["landed"] is False

    r = client.get("/panel/partials/funnel")
    assert "已落地 landed" in r.text
    # the landed row drills into its source; the unmatched one stays plain
    assert 'href="/panel/sources/seed-src"' in r.text
    assert f'href="/panel/sources/ghost-src"' not in r.text
    assert f"manifest #{m_unmatched}" not in r.text  # ids stay out of the way


# ── 2.3 source-detail backlink ───────────────────────────────────────────────
def test_source_detail_backlink_shows_latest_approved_manifest(session):
    d = _disc("backlink")
    _src("seed-src")
    _man("seed-src", "draft", discovery_id=d,
         updated=BASE + dt.timedelta(days=1))       # earlier draft ignored
    mid_old = _man("seed-src", "approved", discovery_id=d,
                   updated=BASE + dt.timedelta(days=2), model="glm-old")
    mid_new = _man("seed-src", "approved", discovery_id=d,
                   updated=BASE + dt.timedelta(days=3), model="glm-new")

    r = client.get("/panel/sources/seed-src")
    assert r.status_code == 200
    assert "来自发现流水线 from discovery pipeline" in r.text
    assert f"manifest #{mid_new}" in r.text          # latest wins
    assert f"manifest #{mid_old}" not in r.text
    assert "glm-new" in r.text
    assert "/panel/funnel" in r.text                 # drill back to the funnel


def test_source_detail_backlink_absent_without_approved_match(session):
    d = _disc("nobacklink")
    _src("draft-only-src")
    _man("draft-only-src", "draft", discovery_id=d)  # draft != provenance
    _src("clean-src")                                # no manifest at all
    _src("name-mismatch-src")
    _man("name-mismatch-src ", "approved", discovery_id=d)  # trailing space != exact

    for src in ("draft-only-src", "clean-src", "name-mismatch-src"):
        page = client.get(f"/panel/sources/{src}").text
        assert "来自发现流水线" not in page, src
    # unrelated sources keep their detail behavior
    assert client.get("/panel/sources/no-such-source").status_code == 404

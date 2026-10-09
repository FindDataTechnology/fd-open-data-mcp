"""panel-policies-search-pagination: the policy list paginates and searches
server-side — the production catalog hit 2,485 rows and one giant table
froze the browser."""
from __future__ import annotations

from fastapi.testclient import TestClient

from fd_open_data_mcp.models import Concept, CrawlPolicy


def _policy(s, i, name=None, entity_type="stock", enabled=False):
    s.add(CrawlPolicy(
        id=i, name=name or f"p-{i:04d}", enabled=enabled,
        concept_ids=[i], entity_type=entity_type,
        date_policy={"mode": "trailing", "days": 1},
        cron_expr="0 6 * * *"))
    s.commit()


def _rows(html: str) -> int:
    return html.count('class="policy-toggle"') or html.count("<tbody>")


class TestPoliciesPagination:
    def test_first_page_caps_rows_and_shows_nav(self, session, monkeypatch):
        from fd_open_data_mcp.panel.app import app
        client = TestClient(app)
        for i in range(1, 121):            # 120 policies > one page of 50
            _policy(session, i)
        r = client.get("/panel/policies")
        assert r.status_code == 200
        import re as _re
        assert len(_re.findall(r'href="/panel/policies/\d+"', r.text)) == 50   # one name link per row
        assert "120 条 policies" in r.text
        assert "第 1/3 页" in r.text
        assert "下一页 ›</a>" in r.text and "上一页" not in r.text

    def test_page_two_offsets(self, session):
        from fd_open_data_mcp.panel.app import app
        client = TestClient(app)
        for i in range(1, 121):
            _policy(session, i)
        r = client.get("/panel/policies?page=2")
        assert r.status_code == 200
        assert "第 2/3 页" in r.text

    def test_search_by_name_fragment(self, session):
        from fd_open_data_mcp.panel.app import app
        client = TestClient(app)
        _policy(session, 1, name="macro-cpi-daily")
        _policy(session, 2, name="legal-docs")
        r = client.get("/panel/policies?q=cpi")
        assert r.status_code == 200
        assert "macro-cpi-daily" in r.text
        assert "legal-docs" not in r.text
        assert "1 / 2 条" in r.text           # filtered / total
        assert 'value="cpi"' in r.text          # search term persists in the input

    def test_search_by_numeric_id(self, session):
        from fd_open_data_mcp.panel.app import app
        client = TestClient(app)
        for i in range(1, 6):
            _policy(session, i)
        r = client.get("/panel/policies?q=3")
        assert r.status_code == 200
        assert "p-0003" in r.text
        assert "p-0001" not in r.text

    def test_search_no_match(self, session):
        from fd_open_data_mcp.panel.app import app
        client = TestClient(app)
        _policy(session, 1)
        r = client.get("/panel/policies?q=zzz-none")
        assert r.status_code == 200
        assert "没有匹配的策略" in r.text
        assert "0 / 1 条" in r.text

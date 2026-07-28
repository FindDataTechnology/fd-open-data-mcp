"""Real end-to-end: read(price.close, <CN stock>, <recent day>) via the real dispatch path."""
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.network


def test_real_read_price_close(session, is_finite):
    pytest.importorskip("akshare")
    from fd_open_data_mcp.catalog.importer import import_provider
    from fd_open_data_mcp.entities.resolver import add_identifier
    from fd_open_data_mcp.fetch.dispatch import read
    from fd_open_data_mcp.models import Concept
    from fd_open_data_mcp.semantic.bindings import propose_bindings
    from fd_open_data_mcp.semantic.concepts import consume_indicator_defs

    import_provider("akshare", session)
    consume_indicator_defs(session)
    propose_bindings(session)
    add_identifier(session, "stock", 1, "akshare", "600519")

    c = session.query(Concept).filter_by(code="price.close").first()
    assert c is not None

    date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y%m%d")
    res = read(session, c.id, "stock", 1, [date])
    assert len(res) == 1
    # the real fetch may return a value, or "no source succeeded" if the date/format misses -
    # the point is the real dispatch path ran end-to-end without crashing.
    if res[0].get("value") is not None:
        assert is_finite(res[0]["value"]), f"value={res[0]['value']}"

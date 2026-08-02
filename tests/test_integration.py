"""Integration test: full pipeline end-to-end with a mocked fetch runner."""
import pandas as pd

import fd_open_data_mcp.fetch.dispatch as dispatch_mod
import fd_open_data_mcp.fetch.instrumentation as instr_mod
from fd_open_data_mcp.catalog.importer import import_provider
from fd_open_data_mcp.entities.resolver import add_identifier, seed_stock_identifiers
from fd_open_data_mcp.fetch.dispatch import read
from fd_open_data_mcp.models import Concept, Schedule, SemanticObservation
from fd_open_data_mcp.refresh.scheduler import generate_schedules
from fd_open_data_mcp.semantic.bindings import propose_bindings
from fd_open_data_mcp.semantic.concepts import consume_indicator_defs


def test_full_pipeline(session, monkeypatch):
    df = pd.DataFrame({"日期": ["2024-07-26"], "收盘": [1850.0]})
    monkeypatch.setattr(instr_mod, "run_upstream", lambda s, c, p: df)

    # 1. import akshare catalog
    import_provider("akshare", session)
    # 2. consume indicator_defs as concepts
    consume_indicator_defs(session)
    # 3. propose column->concept bindings (收盘 -> price.close)
    propose_bindings(session)
    # 4. seed entities (best-effort; fd-entities-indicators may be absent)
    try:
        seed_stock_identifiers(session)
    except Exception:
        pass
    # ensure our test entity has an akshare identifier
    add_identifier(session, "stock", 1, "akshare", "600519")

    # 5. price.close concept exists
    c = session.query(Concept).filter_by(code="price.close").first()
    assert c is not None

    # 6. generate schedules
    gen = generate_schedules(session)
    assert session.query(Schedule).filter_by(concept_id=c.id).first() is not None

    # 7. read -> dispatch (mocked) -> cache
    res = read(session, c.id, "stock", 1, ["2024-07-26"])
    assert res[0]["value"] == 1850.0
    assert session.query(SemanticObservation).filter_by(
        concept_id=c.id, date="2024-07-26").first() is not None

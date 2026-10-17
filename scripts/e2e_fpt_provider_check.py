#!/usr/bin/env python3
"""Cross-service check: fpt's market-data provider reads a just-filled window
(strategy-weight-mode x astock-price-on-demand, task 8.2).

``FdOpenDataMarketDataProvider.get_daily_bars`` goes through the same
``dispatch.read_range`` that gap filling augments. This drill proves the two
halves actually meet: after a cold symbol is filled, the **consumer-facing**
provider returns the full window — not merely that our own helper sees rows.

Why this is separate from 8.1: 8.1 exercises the fill capability on its own
terms; this one exercises it through another repository's code path, which is
the only way to know a consumer gets the benefit. It runs against a scratch DB
the script creates, so it never touches production data, and it fetches real
prices so the window is a real one.

Usage:
    python scripts/e2e_fpt_provider_check.py
Exit codes: 0 success / 1 failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# The consumer under test lives in a sibling repo.
FPT = REPO.parent / "fd-paper-trading"
sys.path.insert(0, str(FPT / "src"))

SYMBOL = "000001"
CONCEPT_CODE = "price.close"
DAYS = 730


def main() -> int:
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

    from fd_open_data_mcp import db as dbmod
    from fd_open_data_mcp.models import Base, Concept, Entity

    # A scratch DB: both services use the shared singleton, so pointing it here
    # keeps this drill away from any real data.
    db_path = Path("/tmp") / "e2e_fpt_provider.db"
    if db_path.exists():
        db_path.unlink()
    os.environ["FD_OPEN_DATA_MCP_DATABASE_URL"] = f"sqlite:///{db_path}"
    dbmod.reset_database()
    db = dbmod.get_database()
    Base.metadata.create_all(db.engine)
    s = db.get_session()

    concept = Concept(code=CONCEPT_CODE, entity_type="stock",
                      frequency="daily", unit="currency", verified=True)
    s.add(concept)
    # The provider resolves a ticker through the entity registry, so the symbol
    # must exist as an entity for the consumer path to reach our rows.
    s.add(Entity(entity_type="stock", code=SYMBOL, name_en="Ping An Bank"))
    s.commit()
    entity_id = s.query(Entity).filter_by(code=SYMBOL).one().id

    from fd_open_data_mcp.fetch.gapfill import fill_gaps, series_dates

    end = date.today().isoformat()
    start = (date.today() - timedelta(days=DAYS)).isoformat()

    sys.path.insert(0, str(REPO / "scripts"))
    from e2e_price_fill import _fetch_days

    closes = _fetch_days(DAYS)
    before = len(series_dates(s, concept.id, "stock", entity_id))
    filled = fill_gaps(
        s, concept.id, "stock", entity_id, start, end,
        fetch_range=lambda a, b: (
            {d: v for d, v in closes.items() if a <= d <= b}, "akshare"
        ),
    )
    stored = len(series_dates(s, concept.id, "stock", entity_id))
    s.close()

    # Now read it back through fpt's own provider (its real public method).
    from fd_paper_trading.marketdata.fd_open_data import FdOpenDataMarketDataProvider

    provider = FdOpenDataMarketDataProvider(
        concept_code=CONCEPT_CODE, entity_type="stock"
    )
    bars = asyncio.run(
        provider.get_daily_bars(SYMBOL, date.fromisoformat(start),
                                date.fromisoformat(end))
    )

    print(f"dates before fill     : {before}")
    print(f"real rows fetched     : {len(closes)}")
    print(f"fill returned points  : {len(filled['points'])}")
    print(f"dates stored after    : {stored}")
    print(f"fpt provider bars     : {len(bars)}")
    if bars:
        print(f"  first {bars[0].date if hasattr(bars[0], 'date') else ''} "
              f"close={bars[0].close}")
        print(f"  last  {bars[-1].date if hasattr(bars[-1], 'date') else ''} "
              f"close={bars[-1].close}")

    ok = before == 0 and stored > 0 and len(bars) == stored
    print(f"\nconsumer sees the fill: {ok}")
    if not ok:
        print("  ^ the provider must return exactly the rows the fill stored")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

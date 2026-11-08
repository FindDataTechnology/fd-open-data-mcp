"""Backfill / daily-increment A-share OHLCV into the ``astock_daily`` family.

Restores the stopped price pipeline (openspec change add-securities-industry-line
tasks 2.1/2.2). The stored ``astock_daily`` family lives on shard xinru3 only —
the federated view ``semantic_observations_all`` EXCLUDES master-local
``astock_daily`` rows and unions the shard foreign table — so rows are written
to the shard through a writable foreign table on the master (``ft_sem_obs_xinru3_w``,
created here if absent; same out-of-band FDW substrate as the read tables).

复权锚点纪律 (the seam problem, measured 2026-11-07):

  * stored rows are 前复权 as of the pipeline's last fetch (2026-08-06 base);
    for 600000 @2026-07-06 stored open is 8.68 while TODAY's ``qfq`` gives 8.26
    — a dividend since the stop shifted the qfq base ~5%.
  * appending today's ``qfq`` would therefore inject a spurious ~+5% level
    jump at the 2026-07-10 seam (exactly the mixed-caliber defect the
    astock-market-data spec forbids).
  * fix: fetch ``hfq`` (base-stable: independent of fetch date), and scale it
    by a per-symbol constant calibrated on the stored overlap —
    ``const = stored_close(anchor) / hfq_close(anchor)``. The result IS the
    frozen-base forward-adjusted continuation: continuous at the seam and
    smooth across future ex-dates. (Verified: stored values reproduce raw
    prices exactly on the overlap, and raw == hfq-calibrated for post-base
    dates.)
  * symbols with no stored anchor row in the calibration window (new listings,
    long-suspended) fall back to ``qfq`` (base today) — no seam because they
    have no stored rows to seam against.

Other contracts:

  * whitelist filter — writes only symbols passed via ``--symbols`` /
    ``--symbols-file`` (805-code securities whitelist in production);
  * idempotent write — shard3 has NO unique index on the natural key, so
    existing (concept_id, entity_id, date) keys are read first and skipped;
    a re-run inserts 0 rows (只增，不改冻结历史);
  * explicit failure — fetch errors are counted and reported; exit 1 only
    when every leg failed and nothing was written (never a silent empty run);
  * known pre-existing mislabel (documented, not fixed here): concept 238 is
    registered as ``price.amount``/currency but stores 成交量 in 手 (lots) —
    verified against three symbols; this script continues that meaning for
    series consistency. Concept 237 (price.volume) has zero stored history
    and is deliberately not started here.

Run as a crawl-policy direct script (k8s Job) or ad hoc:

    python bulk_ingest_astock_daily.py --db-url <master dsn> \
        --symbols 600000,600519 --start 2026-07-11      # backfill
    python bulk_ingest_astock_daily.py --db-url <master dsn> \
        --symbols-file symbols.txt                       # default window
    ... --verify | --gap-probe | --selfcheck
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import socket
import statistics
import sys
import time

import psycopg2.extras
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("astock-daily")

SOURCE = "astock_daily"
ENTITY_TYPE = "stock"
GRAN = "day"
READ_FT = "ft_sem_obs_xinru3"
WRITE_FT = "ft_sem_obs_xinru3_w"
SERVER = "shard_xinru3"

# concept_id -> Bar field; 238 carries 成交量(手) under the price.amount label
# (pre-existing family convention — see module docstring).
CONCEPTS = {233: "open", 234: "close", 235: "high", 236: "low", 238: "volume"}
FIELD_COL = {"open": "开盘", "close": "收盘", "high": "最高", "low": "最低",
             "volume": "成交量"}
PRICE_FIELDS = ("open", "close", "high", "low")
CONCEPT_IDS = tuple(CONCEPTS)

CREATE_WRITE_FT = f"""
CREATE FOREIGN TABLE IF NOT EXISTS {WRITE_FT} (
  concept_id integer NOT NULL,
  entity_type varchar(32) NOT NULL,
  entity_id integer NOT NULL,
  date varchar(64) NOT NULL,
  date_ts date NOT NULL,
  granularity varchar(8) NOT NULL,
  value varchar(255),
  unit varchar(64),
  source_used varchar(64) NOT NULL,
  fetched_at timestamp NOT NULL
) SERVER {SERVER} OPTIONS (table_name 'semantic_observations')
"""

BATCH = 2000
FETCH_TIMEOUT_S = 90
RETRIES = 3

# Dense, full-history blue chips used only as a trading-calendar probe (the
# same basket the paper-trading astock provider uses). Their rows are
# index-reachable; a market-wide max(date) is not.
CALENDAR_BASKET = ("600000", "600036", "600585", "600900", "601318")

# Hard ceilings so a pathological plan can never hold a shard backend open.
SESSION_OPTS = "-c statement_timeout=120000 -c idle_in_transaction_session_timeout=60000"


def fmt_price(v: float) -> str:
    """Match the stored series' formatting ('8.68', '9', '1204.98')."""
    s = f"{round(float(v), 2):.2f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _fetch_with_alarm(fn, **kw):
    import signal

    def _timeout(signum, frame):
        raise TimeoutError(f"{fn.__name__} timed out")
    old = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(FETCH_TIMEOUT_S)
    try:
        return fn(**kw)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def ensure_write_ft(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_WRITE_FT))


def load_symbols(args) -> list[str]:
    codes: set[str] = set()
    if args.symbols:
        codes.update(c.strip().zfill(6) for c in args.symbols.split(",") if c.strip())
    if args.symbols_file:
        for line in open(args.symbols_file, encoding="utf-8"):
            c = line.strip()
            if c:
                codes.add(c.zfill(6))
    if not codes:
        raise SystemExit("no symbols: pass --symbols or --symbols-file")
    return sorted(codes)


def select_codes(engine, args) -> list[str]:
    """--universe（管道口径）或显式代码表；两者互斥，避免半覆盖被当成恢复。"""
    if args.universe:
        if args.symbols or args.symbols_file:
            raise SystemExit("--universe 与 --symbols/--symbols-file 互斥")
        codes = universe_codes(engine, READ_FT)
        log.info("universe: %d codes from the astock entity set", len(codes))
        return codes
    return load_symbols(args)


#: entity_ids per FDW round trip when asking the shard *which* symbols have rows.
PRESENCE_CHUNK = 50


def _id_chunks(ids: list[int], chunk: int = PRESENCE_CHUNK):
    """Yield the entity_id array in bounded slices.

    Every shard read goes through this. ``semantic_observations`` is a 250-chunk
    hypertable behind FDW: one backend asked to aggregate/scan for thousands of
    ids at once allocates a hash table big enough to hit the container memory
    limit (measured 2026-11-08 — cgroup OOM, anon-rss 2.2GB). The chunk width is
    the knob that keeps that allocation small.
    """
    for i in range(0, len(ids), chunk):
        yield ids[i:i + chunk]


def _entity_has_rows(engine, read_ft: str, eid: int) -> bool:
    """Single-point existence probe: concept_id + one entity_id, LIMIT 1.

    The postmortem's prescribed fallback when a chunked probe blows its timeout
    on a loaded shard.
    """
    with engine.connect() as conn:
        row = conn.execute(text(
            f"SELECT 1 FROM {read_ft} WHERE concept_id=234 "
            "AND entity_id = :id LIMIT 1"), {"id": eid}).fetchone()
    return row is not None


def _ids_with_rows(engine, read_ft: str, ids: list[int], chunk: int = PRESENCE_CHUNK) -> set[int]:
    """Of these entity_ids, which actually hold astock_daily rows.

    Chunked on purpose. The obvious form — ``SELECT DISTINCT entity_id FROM
    {read_ft} WHERE concept_id=234`` — ships every row of the price family
    (~9M) across the FDW connection and dedups here, which is the very
    full-scan shape that OOM'd the shard's 4GB backend on 2026-11-07 (red line:
    shard reads only with concept_id/entity_id predicates). Asking in small
    batches keeps every trip an index lookup on ``ix_so_read`` and transfers
    only the hits.

    Chunk size is a *load* knob, not just a correctness one: measured
    2026-11-08 04:05, one 200-id chunk on the 46M-row FDW was still running at
    the 120s statement_timeout while the shard was loaded (`QueryCanceled`), and
    "日增量管道每天都会在 precheck 卡死". So: smaller chunks (50), and on a
    chunk timeout fall back to per-entity single-point probes rather than
    failing the batch — a presence check must never be the thing that kills an
    incremental run.
    """
    out: set[int] = set()
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        try:
            with engine.connect() as conn:
                # GROUP BY, not DISTINCT: postgres_fdw pushes GROUP BY down to the
                # shard (Remote SQL ends "GROUP BY 1") but does *not* push
                # DISTINCT down, so DISTINCT ships every matching row back over
                # the tunnel — measured 2026-11-08 on PG 16.15/1.1. Per-entity
                # history is ~5.5k rows, so the old shape pulled millions of
                # rows per run just to answer "which ids have rows".
                rows = conn.execute(text(
                    f"SELECT entity_id FROM {read_ft} "
                    "WHERE concept_id=234 AND entity_type='stock' "
                    "AND entity_id = ANY(:ids) GROUP BY entity_id"), {"ids": part}).fetchall()
            out.update(int(r[0]) for r in rows)
        except DBAPIError as e:
            log.warning("presence probe timed out on %d ids (%s); falling back to "
                        "per-entity probes", len(part), type(e).__name__)
            for eid in part:
                if _entity_has_rows(engine, read_ft, eid):
                    out.add(eid)
    return out


def _astock_ident(engine, read_ft: str, codes: list[str] | None = None,
                  allow_new: bool = False) -> list[tuple[str, int]]:
    """(bare code, entity_id) for the astock entity set, in identifier order.

    ``codes=None`` means the whole family: only bare 6-digit A-share codes are
    considered, so akshare's non-A-share identifiers (indices, boards, HK/US)
    never enter the universe. Callers with an explicit code list keep today's
    behaviour — suffixed twins (``600000.SH``) are still candidates, and the
    row-presence filter below decides which of them really belongs to the
    family.

    ``allow_new`` (explicit codes only) keeps a code whose candidates hold no
    rows at all, so an operator can create a series for a code the family has
    never carried (1,825 such codes existed on 2026-11-08 — recent listings).
    Presence still wins when any candidate has rows, and one entity is chosen
    per bare code (the bare identifier preferred over a suffixed twin), so a
    single stock can never end up split across two entities.
    """
    with engine.connect() as conn:
        # JOIN entities: the identifier row's own entity_type is a claim that can
        # be wrong (an industry entity carrying 00712 typed 'stock' made 000712
        # resolve to 医药生物 — measured 2026-11-08).
        ident = conn.execute(text(
            "SELECT i.identifier, i.entity_id FROM entity_source_identifiers i "
            "JOIN entities e ON e.id = i.entity_id "
            "WHERE i.entity_type=:et AND i.source=:src AND e.entity_type=:et "
            "ORDER BY 1"),
            {"et": ENTITY_TYPE, "src": "akshare"}).fetchall()
    wanted = None if codes is None else set(codes)
    pairs: list[tuple[str, int]] = []
    raw_of: dict[int, str] = {}
    for raw, eid in ident:
        raw = str(raw)
        bare = raw.split(".")[0]
        if wanted is None:
            # the raw identifier itself must be a 6-digit A-share code: a
            # suffixed twin (600000.SH) is not a family member
            if not (len(raw) == 6 and raw.isdigit()):
                continue
        elif bare not in wanted:
            continue
        pairs.append((bare, int(eid)))
        raw_of[int(eid)] = raw
    present = _ids_with_rows(engine, read_ft, sorted({eid for _, eid in pairs}))
    kept = [(bare, eid) for bare, eid in pairs if eid in present]
    if not allow_new or wanted is None:
        return kept

    # A code the operator named explicitly may hold no rows yet (a new
    # listing). Keep it instead of dropping it silently — one entity per code,
    # bare identifier preferred over a suffixed twin.
    have = {bare for bare, _ in kept}
    seen: dict[str, tuple[str, int]] = {}
    for bare, eid in pairs:
        if bare in have:
            continue
        cur = seen.get(bare)
        if cur is None or len(raw_of.get(cur[1], "")) > len(raw_of.get(eid, "")):
            seen[bare] = (bare, eid)
    if seen:
        log.info("new series (no stored rows yet): %d codes %s",
                 len(seen), sorted(seen)[:10])
    return kept + [seen[b] for b in sorted(seen)]


def universe_codes(engine, read_ft: str) -> list[str]:
    """Every bare code in the astock entity set — the family itself is the scope.

    A daily increment that covers only a whitelist leaves the rest of the
    family stale, which is not "recovered": the stored series defines what has
    to keep moving. Whitelist-first ordering belongs to the backfill (2.2), not
    to the standing pipeline.
    """
    return sorted({c for c, _ in _astock_ident(engine, read_ft)})


def resolve_entities(engine, read_ft: str, codes: list[str],
                     allow_new: bool = False) -> tuple[dict[str, int], list[str]]:
    """code -> entity_id, restricted to entities that hold astock_daily rows.

    ``allow_new`` (opt-in, see ``--allow-new-series``) also accepts a code whose
    entities hold no rows at all, so an explicitly named new listing can create
    its series instead of being reported as "no stored rows" and skipped.

    The astock entity set is a bijection with bare codes (4576 ids <-> 4576
    distinct codes, verified 2026-11-07), so duplicates within the set cannot
    happen; duplicates outside it (e.g. 600000.SH twin entities) are excluded
    by construction, which is exactly what keeps a series unsplit.
    """
    wanted = set(codes)
    m: dict[str, int] = {}
    for bare, eid in _astock_ident(engine, read_ft, list(wanted), allow_new=allow_new):
        if bare in wanted:
            if bare in m and m[bare] != eid:
                log.warning("ambiguous code %s in astock set: %s vs %s — keeping first",
                            bare, m[bare], eid)
                continue
            m[bare] = eid
    missing = sorted(wanted - set(m))
    return m, missing


def stored_anchors(engine, read_ft: str, entity_ids: list[int], cal_lo: str, start: str) -> dict[int, list[tuple[str, float]]]:
    """entity_id -> [(date, close), ...] within the calibration window."""
    if not entity_ids:
        return {}
    out: dict[int, list[tuple[str, float]]] = {}
    for part in _id_chunks(entity_ids):
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT entity_id, date, value FROM {read_ft} "
                "WHERE concept_id=234 AND entity_id = ANY(:ids) "
                "AND date >= :lo AND date < :start ORDER BY entity_id, date"),
                {"ids": part, "lo": cal_lo, "start": start}).fetchall()
        for eid, d, v in rows:
            try:
                value = float(v)
            except (TypeError, ValueError):
                continue
            out.setdefault(int(eid), []).append((str(d), value))
    return out


def existing_keys(engine, read_ft: str, entity_ids: list[int], start: str, end: str) -> set[tuple[int, str]]:
    """Already-stored (concept, entity, date) keys in the window — the idempotency set.

    Chunked for the same reason as every other shard read: one backend answering
    for 4,576 ids at once across 250 hypertable chunks blew past the container's
    memory limit on 2026-11-08 (kernel: ``memory cgroup out of memory: Killed
    process (postgres) anon-rss:2201768kB``). Bounding the id array bounds the
    aggregate's hash table.
    """
    if not entity_ids:
        return set()
    out: set[tuple[int, str]] = set()
    for part in _id_chunks(entity_ids):
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT concept_id, entity_id, date FROM {read_ft} "
                "WHERE concept_id = ANY(:cids) AND entity_id = ANY(:ids) "
                "AND date >= :start AND date <= :end"),
                {"cids": list(CONCEPT_IDS), "ids": part,
                 "start": start, "end": end}).fetchall()
        out.update((int(c), int(e), str(d)) for c, e, d in rows)
    return out


def global_max_date(engine, read_ft: str, basket_ids: list[int],
                    today: str | None = None) -> str | None:
    """Latest stored date, probed over a small dense-symbol basket.

    A bare ``max(date)`` over the 46M-row shard hypertable is a full scan
    through the FDW — measured 2026-11-07 to OOM the shard's 4GB backend
    (the hazard ``visibility/census.py`` warns about: never read shard fact
    tables). The baskets' rows ride ix_so_read, so the probe stays index-bound.

    ``today`` caps the probe: a single future-dated row poisons the whole
    incremental window. Measured 2026-11-08: the scheduled --universe run
    computed ``window 2099-01-03..2026-11-07`` (basket max = a stray
    2099-01-02 row), i.e. start > end, so the pipeline wrote zero rows forever
    while looking healthy.
    """
    if not basket_ids:
        return None
    today = today or datetime.date.today().isoformat()
    with engine.connect() as conn:
        row = conn.execute(text(
            f"SELECT max(date) FROM {read_ft} "
            "WHERE concept_id=234 AND entity_id = ANY(:ids) AND date <= :today"),
            {"ids": basket_ids, "today": today}).fetchone()
    return str(row[0]) if row and row[0] else None


def last_stored_dates(engine, read_ft: str, entity_ids: list[int],
                      floor: str, today: str) -> dict[int, str]:
    """entity_id -> its own last stored date within [floor, today].

    The daily increment used to compute ONE window for the whole family, so any
    symbol that stopped receiving bars (suspension, failed fetch, shard flap)
    fell behind permanently: the run kept asking for yesterday while that
    symbol's gap grew, and only a hand-built backfill could recover it (the
    2026-11-08 repair wrote 1,773,415 rows because 3,836 symbols had drifted).
    This is the same index-bound, chunked shape as every other shard read — one
    ``max(date)`` per bounded id array, never a whole-table aggregate.
    """
    out: dict[int, str] = {}
    if not entity_ids:
        return out
    for part in _id_chunks(entity_ids):
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT entity_id, max(date) FROM {read_ft} "
                "WHERE concept_id=234 AND entity_id = ANY(:ids) "
                "AND date >= :floor AND date <= :today GROUP BY entity_id"),
                {"ids": part, "floor": floor, "today": today}).fetchall()
        for eid, d in rows:
            if d:
                out[int(eid)] = str(d)
    return out


def per_symbol_start(global_start: str, sym_last: str | None, end: str,
                     max_catchup_days: int) -> tuple[str, str]:
    """(start, kind) for one symbol: "current" | "caught_up" | "capped".

    ``sym_last`` is the symbol's own last stored date (None = no rows in the
    lookback, i.e. bounded by the cap). A symbol whose own date is behind the
    run's global start is fetched from there, so the standing run heals drift
    instead of waiting for a manual backfill; the cap keeps one badly lagging
    symbol from turning a daily run into a multi-hour crawl.
    """
    if max_catchup_days <= 0:
        return global_start, "current"
    cap_start = (datetime.date.fromisoformat(end)
                 - datetime.timedelta(days=max_catchup_days - 1)).isoformat()
    if sym_last is None:
        # No rows anywhere in the lookback (a fresh listing, or a symbol whose
        # series is missing): bound the repair at the cap instead of guessing.
        return cap_start, "capped"
    own = (datetime.date.fromisoformat(sym_last)
           + datetime.timedelta(days=1)).isoformat()
    if own >= global_start:
        return global_start, "current"          # current symbol: unchanged
    # Lagging: start at its own next day, never earlier than the cap boundary.
    if own < cap_start:
        return cap_start, "capped"
    return own, "caught_up"


def fetch_frame(code: str, adjust: str, start: str, end: str, failed: list[str]):
    import akshare as ak
    for attempt in range(1, RETRIES + 1):
        try:
            df = _fetch_with_alarm(
                ak.stock_zh_a_hist, symbol=code, period="daily",
                start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                adjust=adjust)
            if df is None or len(df) == 0:
                return None
            return df
        except Exception as e:  # noqa: BLE001 — explicit failure, retried
            log.warning("%s %s fetch attempt %d/%d FAILED: %s", code, adjust, attempt, RETRIES, e)
            if attempt == RETRIES:
                failed.append(f"{code}:{adjust}:{type(e).__name__}")
                return None
    return None


def ingest_symbol(engine, code: str, entity_id: int, units: dict[int, str],
                  anchor_rows: list[tuple[str, float]] | None,
                  start: str, end: str, now, existing: set[tuple[int, str]],
                  dry: bool, stats: dict, failed: list[str]) -> int:
    """Fetch one symbol, calibrate, and return the number of rows written."""
    if anchor_rows:
        anchor_date = max(d for d, _ in anchor_rows)
        adjust = "hfq"
        frame = fetch_frame(code, adjust, anchor_date, end, failed)
        if frame is None:
            return 0
        required = {"日期", FIELD_COL["close"]}
        if not required.issubset(frame.columns):
            failed.append(f"{code}:missing-cols")
            log.error("%s columns drifted: %s", code, frame.columns.tolist()[:6])
            return 0
        hfq = {str(r["日期"]): float(r[FIELD_COL["close"]])
               for _, r in frame.iterrows()
               if r[FIELD_COL["close"]] is not None}
        ratios = [v / hfq[d] for d, v in anchor_rows
                  if d in hfq and hfq[d] and hfq[d] > 0]
        if not ratios:
            log.warning("%s no usable overlap for hfq calibration — qfq fallback", code)
            return ingest_symbol(engine, code, entity_id, units, None,
                                 start, end, now, existing, dry, stats, failed)
        const = statistics.median(ratios)
        eff_from = max(anchor_date, (datetime.date.fromisoformat(start)
                                     - datetime.timedelta(days=1)).isoformat())
        scale = const
    else:
        adjust = "qfq"
        frame = fetch_frame(code, adjust, start, end, failed)
        if frame is None:
            stats["empty_or_failed"] += 1
            return 0
        eff_from = (datetime.date.fromisoformat(start)
                    - datetime.timedelta(days=1)).isoformat()
        scale = 1.0

    if "日期" not in frame.columns:
        failed.append(f"{code}:missing-date-col")
        return 0

    rows = []
    seam_checked = False
    for _, r in frame.iterrows():
        d = str(r["日期"])
        if d <= eff_from:
            continue
        try:
            datetime.date.fromisoformat(d)
        except ValueError:
            continue
        for cid, field in CONCEPTS.items():
            col = FIELD_COL[field]
            if col not in frame.columns:
                continue
            raw = r[col]
            if raw is None or str(raw).strip() in ("", "--", "nan"):
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v != v:
                continue
            if field == "volume":
                value = str(int(round(v)))
            else:
                value = fmt_price(v * scale)
            if (cid, entity_id, d) in existing:
                continue
            rows.append({
                "concept_id": cid, "entity_type": ENTITY_TYPE,
                "entity_id": entity_id, "date": d, "date_ts": d,
                "granularity": GRAN, "value": value, "unit": units.get(cid, ""),
                "source_used": SOURCE, "fetched_at": now,
            })
        # seam guard: first written close vs the LAST stored close before the
        # window. Comparing against max() of the calibration window measured the
        # 10-day high, not the seam, so any symbol that fell into the seam
        # tripped a phantom 25-30% "jump" (000733/000519/000657, 2026-11-08).
        if not seam_checked and anchor_rows and "收盘" in frame.columns:
            c0 = float(r["收盘"]) * (scale if scale else 1.0)
            prev = anchor_rows[-1][1]          # anchor_rows is ORDER BY date
            if prev > 0 and abs(c0 - prev) / prev > 0.25:
                log.warning("%s seam jump %.2f%% at %s (last stored %.2f -> scaled %.2f)",
                            code, abs(c0 - prev) / prev * 100, d, prev, c0)
            seam_checked = True

    if dry:
        stats["rows"] += len(rows)
        return len(rows)
    if rows:
        cols = ["concept_id", "entity_type", "entity_id", "date", "date_ts",
                "granularity", "value", "unit", "source_used", "fetched_at"]
        sql = (f"INSERT INTO {WRITE_FT} ({', '.join(cols)}) VALUES %s")
        vals = [tuple(r[c] for c in cols) for r in rows]
        for bi in range(0, len(vals), BATCH):
            chunk = vals[bi:bi + BATCH]
            with engine.begin() as conn:
                cur = conn.connection.driver_connection.cursor()
                psycopg2.extras.execute_values(cur, sql, chunk, page_size=BATCH)
        stats["rows"] += len(rows)
    return len(rows)


def selfcheck() -> int:
    print(f"selfcheck: concepts {CONCEPTS}")
    print(f"selfcheck: fields {FIELD_COL}")
    print(f"selfcheck: write ft = {WRITE_FT} on server {SERVER}")
    cal = CALENDAR_FILE
    print(f"selfcheck: calendar {cal} exists={cal.exists()}")
    print("selfcheck OK")
    return 0


# ------------------------------------------------------------------ gap probe
# 2.2 的验收工具。交易日历**必须独立于被检数据**：若用「数据里出现过的日期
# 并集」当日历，整市场断档（正是 2026-07-10 之后发生的事）会把自己判成非交易
# 日，探针永远绿灯。日历来自 akshare 的新浪交易日历，落成仓内夹具。

CALENDAR_FILE = Path(__file__).resolve().parent / "data" / "cn_trade_cal_2026.txt"


def load_calendar(path: str, start: str, end: str) -> list[str]:
    """读交易所日历夹具，截取 [start, end] 内的交易日（升序）。"""
    days: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if start <= s <= end:
                days.append(s)
    return days


def gap_report(expected: list[str], stored: dict[int, set[str]]) -> dict:
    """纯函数：算缺口（无 DB，可单测）。

    - ``global_missing``：**任何标的都没有**的交易日（整市场断档，硬失败）
    - ``partial``：有数据但缺若干交易日（可能是停牌/新上市，也可能是漏采——
      无停牌日历不可判定，交人工认领）
    - ``empty``：窗口内一行都没有（新上市/长期停牌/整段漏采）
    """
    covered: set[str] = set()
    for dates in stored.values():
        covered |= dates
    exp = set(expected)
    return {
        "global_missing": [d for d in expected if d not in covered],
        "empty": sorted(eid for eid, dates in stored.items() if not dates),
        "partial": sorted(
            (eid, sorted(exp - dates))
            for eid, dates in stored.items()
            if dates and (exp - dates)
        ),
        "ok": sorted(eid for eid, dates in stored.items() if dates and not (exp - dates)),
    }


def stored_dates(engine, read_ft: str, entity_ids: list[int], start: str, end: str) -> dict[int, set[str]]:
    """按标的取窗口内已存日期（concept 234=收盘 作存在性标记；走 ix_so_read）。

    Chunked: the gap probe asks for the whole universe, and a single 805-id
    aggregate over 250 hypertable chunks is what OOM-killed the shard's backend
    on 2026-11-08 (cgroup limit did its job — host stayed up, one container
    restarted). Width is the memory knob.
    """
    out: dict[int, set[str]] = {eid: set() for eid in entity_ids}
    if not entity_ids:
        return out
    for part in _id_chunks(entity_ids):
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT entity_id, date FROM {read_ft} "
                "WHERE concept_id = 234 AND entity_id = ANY(:ids) "
                "AND date >= :start AND date <= :end GROUP BY entity_id, date"),
                {"ids": part, "start": start, "end": end}).fetchall()
        for eid, d in rows:
            out.setdefault(int(eid), set()).add(str(d))
    return out


def gap_probe(engine, read_ft: str, code_map: dict[str, int], start: str, end: str,
              calendar_file: str, strict_symbols: bool = False) -> int:
    expected = load_calendar(calendar_file, start, end)
    if not expected:
        log.error("calendar %s has no trading days in %s..%s — 先修日历夹具再判缺口",
                  calendar_file, start, end)
        return 2
    stored = stored_dates(engine, read_ft, sorted(code_map.values()), start, end)
    rep = gap_report(expected, stored)

    log.info("gap probe: window %s..%s, %d trading days, %d symbols",
             start, end, len(expected), len(code_map))
    log.info("stored entity-days: %d", sum(len(v) for v in stored.values()))
    log.info("GLOBAL missing trading days: %d%s", len(rep["global_missing"]),
             (" -> " + str(rep["global_missing"][:12])) if rep["global_missing"] else "")
    log.info("symbols ok=%d partial=%d empty=%d", len(rep["ok"]), len(rep["partial"]), len(rep["empty"]))
    for eid, missing in rep["partial"][:10]:
        log.warning("partial entity %d: missing %d days (first 5: %s)",
                    eid, len(missing), missing[:5])
    if rep["empty"]:
        log.warning("empty entities (no rows in window): %d (first 20: %s)",
                    len(rep["empty"]), rep["empty"][:20])

    if rep["global_missing"]:
        log.error("VERDICT: FAIL — 整市场存在断档交易日（硬失败）")
        return 1
    if strict_symbols and rep["partial"]:
        log.error("VERDICT: FAIL — %d 个标的窗口内有缺口（--strict-symbols）", len(rep["partial"]))
        return 1
    log.info("VERDICT: PASS — 全市场无断档；%d 个标的的空洞需人工认领（停牌/新上市 vs 漏采）",
             len(rep["partial"]) + len(rep["empty"]))
    return 0



def shard_endpoint(engine, ft_name: str) -> tuple[str, int] | None:
    """(host, port) of the shard behind a foreign table, from the local catalog.

    Pure catalog read — no FDW traffic, so it stays safe while the shard is down.
    """
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT s.srvoptions FROM pg_foreign_table t "
            "  JOIN pg_class c ON c.oid = t.ftrelid "
            "  JOIN pg_foreign_server s ON s.oid = t.ftserver "
            " WHERE c.relname = :name"), {"name": ft_name}).fetchone()
    if not row or not row[0]:
        return None
    opts = dict(o.split("=", 1) for o in row[0] if "=" in o)
    host, port = opts.get("host"), opts.get("port")
    if not host:
        return None
    try:
        return host, int(port or 5432)
    except ValueError:
        return None


def assert_shard_reachable(engine, ft_name: str, timeout: float = 5.0) -> None:
    """Refuse to start when the shard is not answering on its socket.

    A black-holed shard blocks the FDW call inside PQexec, where statement_timeout
    never fires. Measured 2026-11-08: ``set statement_timeout='15000'`` failed to
    interrupt even a single-entity indexed read (master backend pinned in
    wait_event_type=Extension), one COMMIT sat 851s, and the scheduled 3h job died
    with "pod reclaimed, no terminal state to grab". A 5s socket probe turns that
    multi-hour hang into one explicit error line.
    """
    ep = shard_endpoint(engine, ft_name)
    if not ep:
        log.info("shard endpoint for %s unknown; skipping reachability probe", ft_name)
        return
    host, port = ep
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as e:
        log.error("FAILED: shard %s (%s:%s) unreachable: %s — refusing to start; "
                  "an FDW call would hang in PQexec where statement_timeout does "
                  "not apply", ft_name, host, port, e)
        raise SystemExit(3)
    log.info("shard %s reachable at %s:%s", ft_name, host, port)


def slice_codes(codes: list[str], spec: str) -> list[str]:
    """Take the I-th of N contiguous, non-overlapping slices (``I/N``, 1-based).

    The scheduled universe run has to be split into N pods that together cover
    every code exactly once — that is the postmortem's "按 code 段切 Job" (the
    4GB shards cannot absorb one pod doing all 4,576 symbols). Deterministic and
    order-stable so a re-run of the same slice is idempotent.
    """
    try:
        i_s, n_s = spec.split("/")
        idx, total = int(i_s), int(n_s)
    except (ValueError, AttributeError):
        raise SystemExit(f"--slice wants I/N (e.g. 1/8), got {spec!r}")
    if total < 1 or not 1 <= idx <= total:
        raise SystemExit(f"--slice I/N out of range: {spec!r}")
    # ceil-divide so no slice is empty when there are more slices than codes
    size = -(-len(codes) // total)
    return codes[(idx - 1) * size: idx * size]


def slice_groups(codes: list[str], n: int) -> list[tuple[str, list[str]]]:
    """Contiguous ``[(label, codes)]`` covering ``codes`` exactly once, in order.

    Same partitioning as :func:`slice_codes`, so ``--slice-all N`` walks exactly
    the code sets that ``--slice I/N`` would each run. Empty slices are dropped
    (fewer codes than slices), and the labels stay ``k/N`` 1-based so logs line
    up with a manual sliced run.
    """
    ordered = sorted(codes)
    n = max(1, min(int(n), len(ordered) or 1))
    size = -(-len(ordered) // n)
    groups = [(f"{k + 1}/{n}", ordered[k * size:(k + 1) * size]) for k in range(n)]
    return [g for g in groups if g[1]]


def _report_yield(attempted: int, new: int) -> None:
    """Tell the reconciler's ledger that rows actually landed.

    Direct-executor pods get ``SCRAW_JOB_REF`` injected, and the reconciler
    classifies a finished job from the counters the pod wrote — *absent*
    counters close as ``zero_yield`` even when the process ingested plenty
    (measured 2026-11-08: the 8-slice scheduled run wrote nothing because its
    window was a weekend, but a run that DOES land data would have been
    reported the same way). ``report_run_yield`` no-ops when the env is absent,
    so local/ad-hoc runs are unaffected, and it never raises.
    """
    if attempted <= 0 and new <= 0:
        return
    try:
        from fd_open_data_mcp.refresh.yield_report import report_run_yield
        report_run_yield(attempted, new)
    except Exception:  # noqa: BLE001 - accounting must never fail a landed ingest
        log.warning("yield report failed (attempted=%s new=%s)", attempted, new,
                    exc_info=True)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url")
    ap.add_argument("--symbols")
    ap.add_argument("--symbols-file")
    ap.add_argument("--universe", action="store_true",
                    help="覆盖 astock 实体全集（管道口径；与 --symbols/--symbols-file 互斥）")
    ap.add_argument("--slice", metavar="I/N",
                    help="只跑全集/N 的第 I 片（管道切 Job 用；--universe 必须配它）")
    ap.add_argument("--slice-all", type=int, metavar="N",
                    help="依次跑完全部 N 片（单 Job 串行；写完一片睡 --slice-sleep 秒）。"
                         "这是管道口径的推荐形态：全程只有一个写入者，不会叠加并发砸分片")
    ap.add_argument("--slice-sleep", type=float, default=20.0, metavar="S",
                    help="--slice-all 片间冷却秒数（默认 20）")
    ap.add_argument("--allow-full-universe", action="store_true",
                    help="允许 --universe 不切片（4G 分片上不建议，见 2026-11-08 事故）")
    ap.add_argument("--max-catchup-days", type=int, default=30, metavar="N",
                    help="按票自身缺口取数时最多回溯的天数（默认 30；0 = 关闭，回到"
                         "全族单窗口）。落后的票每次补 N 天，多跑几次自愈。")
    ap.add_argument("--allow-new-series", action="store_true",
                    help="显式指定的代码即使家族里没有行也建序列（新股扩面）；"
                         "--universe 不受影响，永远只跑已有序列")
    ap.add_argument("--start", help="YYYY-MM-DD (default: stored max + 1 day)")
    ap.add_argument("--end", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--calibration-days", type=int, default=12)
    ap.add_argument("--limit", type=int, help="smoke: first N symbols only")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="fetch + compare with stored, write nothing")
    ap.add_argument("--gap-probe", action="store_true",
                    help="查询已存日期与交易所日历比对缺口（只读，2.2 验收）")
    ap.add_argument("--calendar-file", default=str(CALENDAR_FILE),
                    help="交易日历夹具（默认仓内 data/cn_trade_cal_2026.txt）")
    ap.add_argument("--strict-symbols", action="store_true",
                    help="--gap-probe 时把任何标的级空洞也判失败（默认只硬判全市场断档）")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()
    # The DSN normally arrives in the environment: the launcher stopped putting
    # it on the command line (it leaked into the pod spec and /proc/cmdline).
    # --db-url stays supported for deliberate local runs and backfills.
    db_url = args.db_url or os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL")
    if not db_url:
        ap.error("no database URL: pass --db-url or set "
                 "FD_OPEN_DATA_MCP_DATABASE_URL (launched Jobs use the latter)")

    socket.setdefaulttimeout(60)
    engine = create_engine(db_url,
                           connect_args={"connect_timeout": 10, "options": SESSION_OPTS},
                           pool_pre_ping=True, pool_recycle=300)

    # Fail fast when the shard itself is mute, before any FDW traffic can park a
    # backend in PQexec for hours (see assert_shard_reachable).
    assert_shard_reachable(engine, READ_FT)

    if args.gap_probe:
        codes = select_codes(engine, args)
        # The gap probe measures staleness of EXISTING series, so a code with no
        # rows is out of scope here (it would read as a 100% hole, not a gap).
        code_map, missing = resolve_entities(engine, READ_FT, codes)
        start = args.start or "2026-07-11"
        end = args.end or datetime.date.today().isoformat()
        log.info("gap probe: %d/%d codes resolved to astock entities",
                 len(code_map), len(codes))
        return gap_probe(engine, READ_FT, code_map, start, end,
                         args.calendar_file, args.strict_symbols)

    ensure_write_ft(engine)

    codes = select_codes(engine, args)
    if args.limit:
        codes = codes[:args.limit]
    if args.slice:
        codes = slice_codes(codes, args.slice)
    # A full-universe run is a load event, not a crawl: 4,576 symbols means ~92
    # presence chunks and 4,576 akshare calls in one pod, and on the 4GB shards
    # that shape is what froze xinru3 on 2026-11-08 (two writers + read
    # amplification). Scheduled runs must therefore be sliced; --universe alone
    # has to be asked for explicitly.
    if args.universe and not (args.slice or args.slice_all or args.limit
                              or args.allow_full_universe):
        log.error("FAILED: --universe without --slice/--slice-all/--limit is not allowed on "
                  "this shard (4GB, no headroom): a full-range universe run is what "
                  "froze xinru3 on 2026-11-08. Pass --slice-all N (walk every slice "
                  "sequentially in this one job) or --slice I/N / --allow-full-universe.")
        return 2
    log.info("symbols: %d", len(codes))

    code_map, missing = resolve_entities(engine, READ_FT, codes,
                                          allow_new=args.allow_new_series)
    log.info("resolved %d entities in the astock universe; %d codes without stored rows (qfq path)",
             len(code_map), len(missing))
    if missing:
        log.info("no-stored-row codes (first 10): %s", missing[:10])

    basket = [code_map[c] for c in CALENDAR_BASKET if c in code_map] or sorted(code_map.values())[:5]
    maxd = global_max_date(engine, READ_FT, basket)
    start = args.start or ((datetime.date.fromisoformat(maxd) + datetime.timedelta(days=1)).isoformat()
                           if maxd else "2026-07-11")
    end = args.end or datetime.date.today().isoformat()
    cal_lo = (datetime.date.fromisoformat(start)
              - datetime.timedelta(days=args.calibration_days)).isoformat()
    log.info("window %s..%s (calibration window from %s; basket max %s over %d symbols)",
             start, end, cal_lo, maxd, len(basket))

    # An inverted window (start > end) is "already current", not an error — but
    # it must say so out loud: the 2026-11-08 scheduled run silently ran
    # 2099-01-03..2026-11-07 and reported a healthy "done: 0 rows".
    if start > end:
        log.info("window already current (start %s > end %s); nothing to ingest", start, end)
        log.info("done: 0 rows%s, 0 fetch failures, 0 no-data symbols",
                 " (dry-run)" if (args.dry_run or args.verify) else "")
        return 0

    with engine.connect() as conn:
        units = {int(r.id): r.unit for r in conn.execute(text(
            "SELECT id, unit FROM concepts WHERE id = ANY(:cs)"),
            {"cs": list(CONCEPT_IDS)}).fetchall()}

    anchors = stored_anchors(engine, READ_FT, sorted(code_map.values()), cal_lo, start)
    log.info("calibration anchors: %d symbols have stored rows in the window", len(anchors))

    existing = set() if args.dry_run else existing_keys(
        engine, READ_FT, sorted(code_map.values()), start, end)
    log.info("existing keys in window: %d (idempotent skip)", len(existing))

    # Per-symbol window (add-astock-per-symbol-window): a symbol that fell
    # behind is fetched from its own last stored date instead of waiting for a
    # manual backfill; --start pins one window for everyone when given.
    sym_starts: dict[int, str] = {}
    last_by_eid: dict[int, str] = {}
    if args.start or args.max_catchup_days <= 0:
        log.info("window: global %s..%s for every symbol (per-symbol catch-up %s)",
                 start, end, "disabled" if args.max_catchup_days <= 0 else "pinned by --start")
    else:
        floor = (datetime.date.fromisoformat(end)
                 - datetime.timedelta(days=max(args.max_catchup_days, 1) + 400)).isoformat()
        last_by_eid = last_stored_dates(engine, READ_FT, sorted(code_map.values()), floor, end)
        kinds = {"current": 0, "caught_up": 0, "capped": 0}
        late: list[str] = []
        for code, eid in code_map.items():
            sym_start, kind = per_symbol_start(start, last_by_eid.get(eid), end,
                                               args.max_catchup_days)
            sym_starts[eid] = sym_start
            kinds[kind] += 1
            if kind != "current":
                late.append(f"{code}:{last_by_eid.get(eid) or 'none'}->{sym_start}")
        log.info("window: %d current, %d caught up, %d capped at %dd (global start %s)",
                 kinds["current"], kinds["caught_up"], kinds["capped"],
                 args.max_catchup_days, start)
        if late:
            log.info("caught-up symbols (first 10): %s", late[:10])

    now = datetime.datetime.utcnow()
    failed: list[str] = []
    stats = {"rows": 0, "empty_or_failed": 0}

    # --slice-all N walks the code list in N sequential slices inside this one
    # process: one writer for the whole shard, with a cooldown between slices.
    # Slicing exists because a 4,576-symbol burst plus a concurrent second
    # writer is exactly what froze xinru3 on 2026-11-08. Sequential is the
    # point — do not turn this into parallel workers.
    ordered = sorted(code_map)
    if args.slice_all:
        if args.slice_all < 1:
            log.error("FAILED: --slice-all wants a positive slice count")
            return 2
        groups = slice_groups(ordered, args.slice_all)
        log.info("slice-all: %d symbols in %d sequential slices (%.0fs cooldown)",
                 len(ordered), len(groups), args.slice_sleep)
    else:
        groups = [("1/1", ordered)]

    for gi, (label, grp) in enumerate(groups):
        if gi:
            log.info("slice %s: cooling down %.0fs before next slice",
                     groups[gi - 1][0], args.slice_sleep)
            time.sleep(max(0.0, args.slice_sleep))
            now = datetime.datetime.utcnow()  # fetched_at should track real time
        rows_before = stats["rows"]
        for i, code in enumerate(grp, 1):
            ingest_symbol(engine, code, code_map[code], units, anchors.get(code_map[code]),
                          sym_starts.get(code_map[code], start), end, now, existing,
                          args.dry_run or args.verify, stats, failed)
            if i % 50 == 0 or i == len(grp):
                log.info("slice %s [%d/%d] cumulative rows %s, failed %d",
                         label, i, len(grp), stats["rows"], len(failed))
        if len(groups) > 1:
            log.info("slice %s done: +%d rows", label, stats["rows"] - rows_before)
        # report per slice: a multi-hour 8-slice run must not lose the whole
        # ledger to a crash in the last slice, and the reconciler's snapshot
        # reads these counters as live progress.
        _report_yield(stats["rows"] - rows_before, stats["rows"] - rows_before)

    log.info("done: %d rows%s, %d fetch failures, %d no-data symbols",
             stats["rows"], " (dry-run)" if (args.dry_run or args.verify) else "",
             len(failed), stats["empty_or_failed"])
    for f in failed[:20]:
        log.error("FAILED: %s", f)
    if stats["rows"] == 0 and len(failed) >= len(code_map) and len(code_map) > 0:
        log.error("every leg failed — exiting 1 (explicit failure, not an empty success)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

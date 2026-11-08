"""2.2 缺口探针的纯逻辑单测（无 DB、无网络）。

探针的判定必须对准「断档」本身：日历独立于被检数据，否则整市场断档会把自己
判成非交易日而永远绿灯。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bulk_ingest_astock_daily.py"


def _load():
    spec = importlib.util.spec_from_file_location("astock_daily", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_load_calendar_filters_window_and_comments(mod, tmp_path):
    cal = tmp_path / "cal.txt"
    cal.write_text(
        "# comment\n2026-07-09\n2026-07-10\n2026-07-13\n2026-09-30\n2026-10-01\n",
        encoding="utf-8",
    )
    assert mod.load_calendar(str(cal), "2026-07-10", "2026-09-30") == [
        "2026-07-10",
        "2026-07-13",
        "2026-09-30",
    ]


def test_repo_calendar_fixture_covers_backfill_window(mod):
    days = mod.load_calendar(str(mod.CALENDAR_FILE), "2026-07-11", "2026-12-31")
    assert len(days) > 100, days[:5]
    assert days == sorted(days)
    assert "2026-10-01" not in days  # 国庆休市
    assert "2026-07-13" in days


def test_gap_report_detects_market_wide_gap(mod):
    expected = ["2026-07-10", "2026-07-13", "2026-07-14", "2026-07-15"]
    # 两个标的都缺 07-14/07-15 = 整市场断档（正是被检的那段）
    stored = {
        1: {"2026-07-10", "2026-07-13"},
        2: {"2026-07-10", "2026-07-13"},
    }
    rep = mod.gap_report(expected, stored)
    assert rep["global_missing"] == ["2026-07-14", "2026-07-15"]
    assert rep["partial"] == [(1, ["2026-07-14", "2026-07-15"]),
                              (2, ["2026-07-14", "2026-07-15"])]
    assert rep["ok"] == [] and rep["empty"] == []


def test_gap_report_clean_window_passes(mod):
    expected = ["2026-07-10", "2026-07-13"]
    stored = {1: set(expected), 2: set(expected)}
    rep = mod.gap_report(expected, stored)
    assert rep["global_missing"] == []
    assert rep["ok"] == [1, 2]
    assert rep["partial"] == [] and rep["empty"] == []


def test_gap_report_separates_empty_from_partial(mod):
    expected = ["2026-07-10", "2026-07-13", "2026-07-14"]
    stored = {
        1: set(),                                     # 新上市/长期停牌/整段漏采
        2: {"2026-07-10"},                            # 部分缺
        3: set(expected),                             # 完整
    }
    rep = mod.gap_report(expected, stored)
    assert rep["empty"] == [1]
    assert rep["partial"] == [(2, ["2026-07-13", "2026-07-14"])]
    assert rep["ok"] == [3]
    assert rep["global_missing"] == []


def test_gap_probe_exit_codes(mod, tmp_path, monkeypatch):
    cal = tmp_path / "cal.txt"
    cal.write_text("2026-07-10\n2026-07-13\n", encoding="utf-8")
    expected = ["2026-07-10", "2026-07-13"]

    class _Engine:
        pass

    monkeypatch.setattr(mod, "stored_dates",
                        lambda *a, **k: {1: set(expected), 2: set(expected)})
    assert mod.gap_probe(_Engine(), "ft", {"600000": 1, "600519": 2},
                         "2026-07-10", "2026-07-13", str(cal)) == 0

    # 整市场断档 -> 1（即便 --strict-symbols 关着）
    monkeypatch.setattr(mod, "stored_dates",
                        lambda *a, **k: {1: {"2026-07-10"}, 2: {"2026-07-10"}})
    assert mod.gap_probe(_Engine(), "ft", {"600000": 1, "600519": 2},
                         "2026-07-10", "2026-07-13", str(cal)) == 1

    # 仅单标的空洞 -> 默认 0（停牌不可判），--strict-symbols 下 1
    monkeypatch.setattr(mod, "stored_dates",
                        lambda *a, **k: {1: set(expected), 2: {"2026-07-10"}})
    assert mod.gap_probe(_Engine(), "ft", {"600000": 1, "600519": 2},
                         "2026-07-10", "2026-07-13", str(cal)) == 0
    assert mod.gap_probe(_Engine(), "ft", {"600000": 1, "600519": 2},
                         "2026-07-10", "2026-07-13", str(cal), strict_symbols=True) == 1

    # 日历窗口为空 -> 2（口径缺失不许当通过）
    assert mod.gap_probe(_Engine(), "ft", {"600000": 1},
                         "2026-08-01", "2026-08-02", str(cal)) == 2


class _Args:
    def __init__(self, universe=False, symbols=None, symbols_file=None):
        self.universe = universe
        self.symbols = symbols
        self.symbols_file = symbols_file


def test_universe_codes_dedups_and_sorts(mod, monkeypatch):
    # 孪生实体（同一裸代码两个 id）只取裸代码一次——管道按代码拉行情
    monkeypatch.setattr(mod, "_astock_ident", lambda *a: [
        ("600519", 11), ("000001", 7), ("600519", 12), ("300750", 9)])
    assert mod.universe_codes(None, "ft") == ["000001", "300750", "600519"]


def test_select_codes_universe_is_mutually_exclusive(mod):
    with pytest.raises(SystemExit):
        mod.select_codes(None, _Args(universe=True, symbols="600519"))


def test_select_codes_delegates_to_symbol_list(mod):
    assert mod.select_codes(None, _Args(symbols="000001,600519")) == ["000001", "600519"]


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return _Rows(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Engine:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _Conn(self._rows)


def test_stored_anchors_groups_rows_per_entity(mod):
    """Regression: the first REAL backfill crashed with KeyError on the first
    anchor row — `out[int(eid)].append(...)` had no list to append to, so the
    hfq anchor path had never actually run (every earlier run had zero anchors).
    Two entities must come back as two lists, in row order, and an unparseable
    value must be skipped rather than poisoning the rest."""
    rows = [
        (207, "2026-06-30", 10.5),
        (207, "2026-07-01", 10.75),
        (208, "2026-06-30", 3.25),
        (209, "2026-06-30", None),      # unparseable -> skipped, not fatal
    ]
    got = mod.stored_anchors(_Engine(rows), "ft_sem_obs_xinru3", [207, 208, 209],
                             "2026-06-29", "2026-07-11")
    assert got == {207: [("2026-06-30", 10.5), ("2026-07-01", 10.75)],
                   208: [("2026-06-30", 3.25)]}


def test_global_max_date_caps_probe_at_today(mod):
    """Regression: the incremental window is derived from the basket's max(date),
    so ONE future-dated row poisons every scheduled run. Measured 2026-11-08: a
    stray 2099-01-02 row made the pipeline compute window 2099-01-03..2026-11-07,
    i.e. start > end, and it wrote zero rows forever while logging a healthy
    "done: 0 rows". The probe must therefore bound itself at today."""
    seen = {}

    class _C:
        def execute(self, sql, params=None):
            seen["sql"] = str(sql)
            seen["params"] = params

            class _R:
                def fetchone(self_inner):
                    return ("2026-11-06",)
            return _R()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _E:
        def connect(self):
            return _C()

    got = mod.global_max_date(_E(), "ft_sem_obs_xinru3", [7, 8],
                             today="2026-11-08")
    assert got == "2026-11-06"
    assert "date <= :today" in seen["sql"]
    assert seen["params"] == {"ids": [7, 8], "today": "2026-11-08"}

    # Omitting today must still bound at the real today, never unbounded.
    mod.global_max_date(_E(), "ft", [1])
    assert seen["params"]["today"] == mod.datetime.date.today().isoformat()


def test_shard_endpoint_parses_catalog_options(mod):
    class _C:
        def execute(self, sql, params=None):
            class _R:
                def fetchone(self_inner):
                    return (["host=100.64.0.7", "port=30432", "dbname=fd_open_data"],)
            return _R()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _E:
        def connect(self):
            return _C()

    assert mod.shard_endpoint(_E(), "ft_sem_obs_xinru3") == ("100.64.0.7", 30432)


def test_assert_shard_reachable_refuses_when_socket_dead(mod, monkeypatch):
    """A mute shard must abort BEFORE any FDW call: blocking inside PQexec is not
    interruptible by statement_timeout, which is how one COMMIT sat 851s and a
    scheduled job died with "pod reclaimed, no terminal state"."""
    monkeypatch.setattr(mod, "shard_endpoint",
                        lambda *a, **k: ("100.64.0.7", 30432))

    def _dead(addr, timeout=None):
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(mod.socket, "create_connection", _dead)
    with pytest.raises(SystemExit) as ei:
        mod.assert_shard_reachable(object(), "ft_sem_obs_xinru3")
    assert ei.value.code == 3

    # ...and passes cleanly when the socket answers.
    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(mod.socket, "create_connection",
                        lambda addr, timeout=None: _Sock())
    assert mod.assert_shard_reachable(object(), "ft_sem_obs_xinru3") is None


def test_assert_shard_reachable_skips_when_endpoint_unknown(mod, monkeypatch):
    monkeypatch.setattr(mod, "shard_endpoint", lambda *a, **k: None)
    # Must not explode just because the catalog lookup found nothing.
    assert mod.assert_shard_reachable(object(), "ft") is None


def test_ids_with_rows_falls_back_to_per_entity_on_timeout(mod):
    """Measured 2026-11-08 04:05: one 200-id presence chunk was still running at
    the 120s statement_timeout on a loaded shard (QueryCanceled). The postmortem's
    verdict: "否则日增量管道每天都会在 precheck 卡死". A presence check must
    therefore degrade to single-point probes, never fail the batch."""
    from sqlalchemy.exc import DBAPIError

    calls = []

    class _C:
        def execute(self, sql, params=None):
            s = str(sql)
            calls.append(s)
            if "ANY" in s:
                raise DBAPIError("sel", {}, Exception("QueryCanceled"))

            class _R:
                def fetchone(self_inner):
                    return (1,) if params["id"] == 2 else None
            return _R()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _E:
        def connect(self):
            return _C()

    got = mod._ids_with_rows(_E(), "ft", [1, 2, 3], chunk=3)
    assert got == {2}
    assert sum("LIMIT 1" in c for c in calls) == 3
    # every fallback probe stays index-bound: concept_id + one entity_id
    assert all("concept_id=234" in c for c in calls if "LIMIT 1" in c)


def test_presence_chunk_is_small_enough_for_a_loaded_shard(mod):
    """The chunk default is a load knob: 200 ids/chunk is what blew the timeout."""
    assert mod.PRESENCE_CHUNK <= 50


def test_slice_codes_partitions_without_gap_or_overlap(mod):
    """Scheduled universe runs must be split across pods ("按 code 段切 Job"):
    one pod doing all 4,576 symbols is what froze the 4GB shard on 2026-11-08.
    N slices must together cover the whole list exactly once."""
    codes = [f"{i:06d}" for i in range(1, 21)]
    parts = [mod.slice_codes(codes, f"{i}/4") for i in range(1, 5)]
    assert [len(p) for p in parts] == [5, 5, 5, 5]
    assert sum(parts, []) == codes          # order-stable, no overlap, no gap
    assert mod.slice_codes(codes[:5], "3/7") == ["000003"]   # more slices than codes


def test_slice_codes_rejects_malformed_spec(mod):
    codes = [f"{i:06d}" for i in range(1, 5)]
    for bad in ("bad", "0/4", "5/4", "1/0"):
        with pytest.raises(SystemExit):
            mod.slice_codes(codes, bad)


def test_effective_batch_size_keeps_checkpoint_continuity(tmp_path):
    """The batch index IS the checkpoint key, so a resume must not renumber it:
    the whitelist checkpoint is 50-wide (key 9 = codes[450:500]); resuming it at
    the new 10-wide throttle default would read key 9 as codes[90:100]."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "batches", SCRIPT.parent / "run_astock_backfill_batches.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    assert m.effective_batch_size({}, 10) == 10                       # fresh phase
    assert m.effective_batch_size({"plan": {"batch_size": 50}}, 10) == 50  # resume


def test_astock_ident_universe_skips_non_ashare_identifiers(mod, monkeypatch):
    """--universe must not drag in akshare's indices/boards: only bare 6-digit
    A-share codes survive, and only those that really hold astock_daily rows."""
    rows = [("000001", 1), ("600000", 2), ("600000.SH", 3),
            ("BK0475", 4), ("sh000300", 5)]
    monkeypatch.setattr(mod, "_ids_with_rows", lambda *a, **k: {1, 2, 3, 4, 5})
    assert mod._astock_ident(_Engine(rows), "ft") == [("000001", 1), ("600000", 2)]


def test_astock_ident_explicit_codes_still_accept_suffixed_twins(mod, monkeypatch):
    """A whitelist code may exist only under a suffixed identifier — keep it."""
    monkeypatch.setattr(mod, "_ids_with_rows", lambda *a, **k: {3})
    assert mod._astock_ident(_Engine([("600000.SH", 3)]), "ft", ["600000"]) == [("600000", 3)]
    # ...while an id with no stored rows is filtered out
    monkeypatch.setattr(mod, "_ids_with_rows", lambda *a, **k: set())
    assert mod._astock_ident(_Engine([("600000", 2)]), "ft", ["600000"]) == []


def test_ids_with_rows_probes_in_bounded_chunks(mod):
    """Presence must be asked in index-bound batches, never as one DISTINCT
    over the whole concept (that shape OOM'd the shard backend, 2026-11-07)."""
    seen = []

    class _E:
        def connect(self):
            engine = self

            class _C:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, sql, params):
                    seen.append(list(params["ids"]))

                    class _R:
                        def fetchall(self_inner):
                            return [(params["ids"][0],)]

                    return _R()

            return _C()

    got = mod._ids_with_rows(_E(), "ft", list(range(1, 8)), chunk=3)
    assert [len(s) for s in seen] == [3, 3, 1]
    assert got == {1, 4, 7}


def test_slice_groups_covers_every_code_once_in_order(mod):
    codes = [f"{i:06d}" for i in range(1, 26)]
    groups = mod.slice_groups(codes, 4)
    assert [label for label, _ in groups] == ["1/4", "2/4", "3/4", "4/4"]
    flat = [c for _, grp in groups for c in grp]
    assert flat == sorted(codes)          # every code exactly once, in order
    assert len(set(flat)) == len(codes)


def test_slice_groups_matches_slice_codes_partition(mod):
    codes = [f"{i:06d}" for i in range(1, 26)]
    for k, (_, grp) in enumerate(mod.slice_groups(codes, 4), 1):
        assert grp == mod.slice_codes(sorted(codes), f"{k}/4")


def test_slice_groups_drops_empty_slices(mod):
    groups = mod.slice_groups(["000001", "000002"], 8)
    assert len(groups) == 2 and all(grp for _, grp in groups)


def test_id_chunks_covers_all_ids_in_bounded_slices(mod):
    ids = list(range(1, 806))
    parts = list(mod._id_chunks(ids, 50))
    assert [len(p) for p in parts][:3] == [50, 50, 50]
    assert sum(len(p) for p in parts) == len(ids)
    assert [i for p in parts for i in p] == ids


# ── --allow-new-series (add-astock-new-series-flag) ──────────────────────────

def test_new_series_flag_off_keeps_dropping_codes_without_rows(mod, monkeypatch):
    """Default behaviour must not change: presence still gates explicit codes."""
    monkeypatch.setattr(mod, "_ids_with_rows", lambda *a, **k: set())
    assert mod._astock_ident(_Engine([("001206", 7)]), "ft", ["001206"]) == []


def test_new_series_flag_keeps_one_entity_per_code(mod, monkeypatch):
    """With allow_new a row-less code survives — once, bare name preferred."""
    monkeypatch.setattr(mod, "_ids_with_rows", lambda *a, **k: set())
    got = mod._astock_ident(_Engine([("001206.SZ", 8), ("001206", 7)]), "ft",
                            ["001206"], allow_new=True)
    assert got == [("001206", 7)]


def test_new_series_flag_does_not_override_presence(mod, monkeypatch):
    """A twin that really holds rows still wins, so a series never splits."""
    monkeypatch.setattr(mod, "_ids_with_rows", lambda *a, **k: {8})
    got = mod._astock_ident(_Engine([("001206", 7), ("001206.SZ", 8)]), "ft",
                            ["001206"], allow_new=True)
    assert got == [("001206", 8)]


def test_new_series_flag_never_grows_the_universe(mod, monkeypatch):
    """--universe (codes=None) keeps the presence filter even with allow_new."""
    monkeypatch.setattr(mod, "_ids_with_rows", lambda *a, **k: set())
    assert mod._astock_ident(_Engine([("001206", 7)]), "ft", None, allow_new=True) == []


def test_resolve_entities_passes_the_flag_through(mod, monkeypatch):
    monkeypatch.setattr(mod, "_astock_ident",
                        lambda eng, ft, codes, allow_new=False: [("001206", 7)] if allow_new else [])
    assert mod.resolve_entities(_Engine([]), "ft", ["001206"]) == ({}, ["001206"])
    m, missing = mod.resolve_entities(_Engine([]), "ft", ["001206"], allow_new=True)
    assert m == {"001206": 7} and missing == []


# ── per-symbol window (add-astock-per-symbol-window) ─────────────────────────

def test_per_symbol_start_catches_up_from_own_last_date(mod):
    """A lagging symbol is fetched from its own last stored date + 1."""
    start, kind = mod.per_symbol_start("2026-11-07", "2026-11-01", "2026-11-08", 30)
    assert start == "2026-11-02" and kind == "caught_up"


def test_per_symbol_start_leaves_current_symbols_alone(mod):
    """A current symbol keeps exactly the global window."""
    start, kind = mod.per_symbol_start("2026-11-07", "2026-11-06", "2026-11-08", 30)
    assert start == "2026-11-07" and kind == "current"


def test_per_symbol_start_caps_a_long_gap(mod):
    """The cap bounds the work; the symbol is reported as capped."""
    start, kind = mod.per_symbol_start("2026-11-07", "2026-01-05", "2026-11-08", 30)
    # 30 days ending 2026-11-08 -> floor 2026-10-10
    assert start == "2026-10-10" and kind == "capped"


def test_per_symbol_start_handles_missing_rows_and_disabled_cap(mod):
    """No stored rows in the lookback = capped; cap 0 disables the feature."""
    start, kind = mod.per_symbol_start("2026-11-07", None, "2026-11-08", 30)
    assert start == "2026-10-10" and kind == "capped"
    start, kind = mod.per_symbol_start("2026-11-07", "2026-01-05", "2026-11-08", 0)
    assert start == "2026-11-07" and kind == "current"


# ── identifier integrity guard (fix-identifier-entity-type-mismatch) ─────────

def test_identifier_integrity_deferred_pairs_are_explicit():
    """The five deferred classes are a documented allowlist, not a silent skip.

    Found 2026-11-08: 1,793 identifiers typed `stock` belonged to **person**
    entities (fund managers), so `512660` resolved to a manager. The rows were
    removed; the guard must fail on any *new* class and report the deferred ones.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "check_identifier_integrity.py"
    spec = importlib.util.spec_from_file_location("ident_integrity", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert ("stock", "person") not in mod.DEFERRED      # the class that bit us
    assert ("stock", "fund") in mod.DEFERRED
    assert ("country", "city") in mod.DEFERRED

"""Smoke coverage for the runner table and the adapter<->runner invariant.

The capability this pins down has three claims that were only partly true:

1. every adapter under ``adapters/`` is reachable through ``run_upstream`` — true
   today, but nothing asserted it, so dropping a source from ``RUNNERS`` while
   keeping its adapter would have gone unnoticed until a crawl spent requests;
2. a failing call names the source and the original error — already true in the
   per-runner wrappers (``"akshare {command} raised: {e}"`` etc.);
3. every integrated source has a basic invocation smoke test — adapters had one
   (``tests/test_adapters.py``), the runner table did not: only ``nbs-gdp`` was
   ever exercised.

These tests need no network and no ``[data]`` extra: they assert the *wiring*,
which is what silently breaks.
"""
import inspect

import pytest

from fd_open_data_mcp.adapters import registered as registered_adapters
from fd_open_data_mcp.errors import FetchError
from fd_open_data_mcp.fetch.runner import RUNNERS, FetchError as RunnerFetchError, run_upstream


def test_runner_table_is_not_empty():
    assert RUNNERS, "RUNNERS is the single source of truth for 'can this source be called'"


@pytest.mark.parametrize("source", sorted(RUNNERS))
def test_every_runner_is_a_callable(source):
    runner = RUNNERS[source]
    assert callable(runner), f"{source} runner is not callable"
    # Runners take (command, params); a signature drift here breaks every plan row.
    params = list(inspect.signature(runner).parameters.values())
    positional = [
        p for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert len(positional) >= 2, f"{source} runner signature is {inspect.signature(runner)}"


def test_every_adapter_source_has_a_runner():
    """An adapter nobody can dispatch to is dead code — and it looks alive."""
    missing = sorted({s for s, _c in registered_adapters()} - set(RUNNERS))
    assert not missing, f"adapters registered for sources with no runner: {missing}"


def test_unknown_source_names_the_miss_and_the_alternatives():
    with pytest.raises(FetchError) as excinfo:
        run_upstream("no-such-source", "anything", {})
    message = str(excinfo.value)
    assert "no runner for source no-such-source" in message
    # The alternatives are the actionable half; each registered source appears.
    for source in RUNNERS:
        assert source in message, f"{source} missing from the alternatives list"


def test_run_upstream_dispatches_to_the_table(monkeypatch):
    calls = []

    def fake(command, params):
        calls.append((command, params))
        return "dispatched"

    monkeypatch.setitem(RUNNERS, "akshare", fake)
    assert run_upstream("akshare", "stock_zh_a_hist", {"symbol": "600519"}) == "dispatched"
    assert calls == [("stock_zh_a_hist", {"symbol": "600519"})]


def test_fetch_error_is_one_class():
    """``runner.FetchError`` must BE ``errors.FetchError``, not a lookalike.

    They used to be two unrelated classes. The adapters imported the ``errors``
    one, so their failures were invisible to ``except FetchError`` in
    ``fetch/dispatch.py`` and ``fetch/instrumentation.py`` — no fetch_log error
    row, no retry/ban classification.
    """
    assert RunnerFetchError is FetchError

    # Both attribute families must land on the same instance.
    exc = FetchError("boom", status_code=429, response_text="rate limited",
                     source="nbs-gdp", command="get_gdp_quarterly")
    assert exc.status_code == 429 and exc.response_text == "rate limited"
    assert exc.source == "nbs-gdp" and exc.command == "get_gdp_quarterly"
    # The ``source.command:`` prefix only applies when both coordinates are set.
    assert str(exc) == "nbs-gdp.get_gdp_quarterly: boom"


def test_errors_side_raise_is_caught_as_runner_side():
    """A raise from an adapter that imports ``errors`` is catchable as runner's.

    This is the exact shape that used to escape: ``adapters/nbs_gdp.py`` imports
    ``errors.FetchError``, while ``fetch/instrumentation.py`` catches the class
    imported from ``fetch.runner``.
    """
    with pytest.raises(RunnerFetchError):
        raise FetchError("nbs-gdp.get_gdp_quarterly: boom",
                         source="nbs-gdp", command="get_gdp_quarterly")

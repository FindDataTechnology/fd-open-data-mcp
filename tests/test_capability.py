"""Static endpoint-capability resolution (spec concept-fetch).

The incident: 5,277,961 ``has no callable`` attempts between 2026-08-30 and
2026-09-16, and ~266,000 failed requests in 24 hours on 2026-09-16 alone — all
for endpoints the installed library does not have. Whether a callable exists is
decidable without any network I/O, so the system should not pay a request to
discover it.

The resolution is tested against fake libraries: the fd-open-data-mcp venv does
not ship akshare (the crawl venv does), so a test that depended on the real
package would be verifying the environment, not the logic.
"""
from __future__ import annotations

import types

import pytest

from fd_open_data_mcp.fetch import capability as cap


def _install_library(monkeypatch, source: str, module) -> None:
    """Make ``_library`` return ``module`` for ``source`` only."""
    monkeypatch.setattr(
        cap, "_library", lambda s: module if s == source else None)


# --- the three outcomes ------------------------------------------------------

def test_a_present_callable_is_resolvable(monkeypatch):
    _install_library(monkeypatch, "akshare",
                     types.SimpleNamespace(stock_zh_a_hist=lambda **kw: None))
    status, reason = cap.check_command("akshare", "stock_zh_a_hist")
    assert status == cap.RESOLVABLE
    assert "stock_zh_a_hist" in reason


def test_an_absent_library_attribute_is_unresolvable(monkeypatch):
    """The reported incident, in one assertion."""
    _install_library(monkeypatch, "akshare",
                     types.SimpleNamespace(stock_zh_a_hist=lambda **kw: None))
    status, reason = cap.check_command("akshare", "option_czce_hist")
    assert status == cap.UNRESOLVABLE
    assert "option_czce_hist" in reason


def test_a_non_callable_attribute_is_unresolvable(monkeypatch):
    """A same-named constant is not a callable endpoint."""
    _install_library(monkeypatch, "akshare", types.SimpleNamespace(__version__="1.18.83"))
    assert cap.check_command("akshare", "__version__")[0] == cap.UNRESOLVABLE


def test_a_library_missing_from_this_environment_is_unverifiable(monkeypatch):
    """Not knowing is not the same as it being broken: a planning host without
    the crawl image's libraries must not be read as 'everything is unresolvable'."""
    monkeypatch.setattr(cap, "_library", lambda s: None)
    status, reason = cap.check_command("akshare", "stock_zh_a_hist")
    assert status == cap.UNVERIFIABLE
    assert "not importable" in reason


def test_a_source_with_no_runner_is_unresolvable():
    assert cap.check_command("no-such-source", "whatever")[0] == cap.UNRESOLVABLE


@pytest.mark.parametrize("source", ["polygon", "datacommons", "nbs-gdp"])
def test_a_provider_module_source_is_unverifiable(source):
    """Resolved through a provider package's module path, with no single library
    attribute to introspect - reported, never excluded."""
    assert cap.check_command(source, "anything")[0] == cap.UNVERIFIABLE


# --- runner parity -----------------------------------------------------------

def test_runner_implemented_commands_resolve_without_a_library(monkeypatch):
    """`run_wbgapi` implements these itself; there is no `wbgapi.list_indicators`
    attribute, so mirroring only the library lookup would call them broken."""
    monkeypatch.setattr(cap, "_library", lambda s: None)
    for command in ("get_indicator_data", "list_indicators", "list_economies",
                    "get_series_metadata"):
        assert cap.check_command("wbgapi", command)[0] == cap.RESOLVABLE


def test_yfinance_ticker_methods_resolve_through_the_class(monkeypatch):
    """`ticker_*` commands are methods on Ticker, not module attributes."""
    class Ticker:
        def history(self):
            ...

    _install_library(monkeypatch, "yfinance", types.SimpleNamespace(Ticker=Ticker))
    assert cap.check_command("yfinance", "ticker_history")[0] == cap.RESOLVABLE
    assert cap.check_command("yfinance", "ticker_nonexistent")[0] == cap.UNRESOLVABLE


def test_edgar_company_methods_resolve_through_the_class(monkeypatch):
    class Company:
        def financials(self):
            ...

    _install_library(monkeypatch, "edgar", types.SimpleNamespace(Company=Company))
    assert cap.check_command("edgar", "company_financials")[0] == cap.RESOLVABLE
    assert cap.check_command("edgar", "company_nope")[0] == cap.UNRESOLVABLE


def test_every_runner_source_is_answerable():
    """`runner.RUNNERS` is the source of truth for which sources exist; the
    capability check must never be stricter than the runner."""
    from fd_open_data_mcp.fetch.runner import RUNNERS

    for source in RUNNERS:
        status, _ = cap.check_command(source, "anything")
        assert status in (cap.RESOLVABLE, cap.UNRESOLVABLE, cap.UNVERIFIABLE)


def test_unverifiable_is_not_treated_as_unresolvable(monkeypatch):
    monkeypatch.setattr(cap, "_library", lambda s: None)
    assert cap.is_unresolvable("akshare", "stock_zh_a_hist") is False
    assert cap.is_unresolvable("akshare", "was_never_a_command") is False


# --- no network I/O ----------------------------------------------------------

def test_resolution_performs_no_network_io(monkeypatch):
    """Statically decidable means no socket, ever - the whole point is to refuse
    a doomed endpoint without discovering it by request."""
    import socket

    def _no_sockets(*a, **kw):
        raise AssertionError("capability check must not open a socket")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)

    for source, command in [
        ("akshare", "stock_zh_a_hist"),
        ("polygon", "get_daily"),
        ("wbgapi", "list_indicators"),
        ("no-such-source", "x"),
    ]:
        cap.check_command(source, command)


# --- report (report-only) ----------------------------------------------------

def test_report_checks_registered_functions_and_writes_nothing(
        session, monkeypatch):
    from fd_open_data_mcp.models import Function, Source

    source = Source(name="akshare", label="akshare")
    session.add(source)
    session.commit()
    session.add_all([
        Function(source_id=source.id, command="stock_zh_a_hist"),
        Function(source_id=source.id, command="option_czce_hist"),
        Function(source_id=source.id, command="stock_zh_a_tick_tx"),
    ])
    session.commit()
    before = session.query(Function).count()

    _install_library(monkeypatch, "akshare",
                     types.SimpleNamespace(stock_zh_a_hist=lambda **kw: None))
    result = cap.report(session)

    assert result["checked"] == 3
    assert result[cap.RESOLVABLE] == 1
    assert result[cap.UNRESOLVABLE] == 2
    assert [r["command"] for r in result["unresolvable_functions"]] == [
        "option_czce_hist", "stock_zh_a_tick_tx"]
    # Report-only: nothing is disabled, excluded or written.
    assert session.query(Function).count() == before
    assert all(f.verified for f in session.query(Function).all())


# --- enforcement at plan time (task 5.1) ------------------------------------

def _seed_candidates(session, commands):
    """A stock concept with one confirmed candidate per command."""
    from fd_open_data_mcp.models import (
        Concept, ConceptBinding, Function, FunctionColumn, Source,
    )

    source = Source(name="akshare", label="akshare")
    session.add(source)
    session.flush()
    concept = Concept(code="price.close", entity_type="stock", measure="",
                      unit="currency", frequency="daily")
    session.add(concept)
    session.flush()
    for command in commands:
        fn = Function(source_id=source.id, command=command)
        session.add(fn)
        session.flush()
        col = FunctionColumn(function_id=fn.id, name=f"收盘-{command}")
        session.add(col)
        session.flush()
        session.add(ConceptBinding(concept_id=concept.id, column_id=col.id,
                                   confidence=0.9, provenance="manual", reviewed=True))
    session.commit()
    return concept


def _plan(session, concept_id):
    from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
    from fd_open_data_mcp.crawl.planner import plan_crawl

    return plan_crawl(session, [concept_id],
                      EntityScope(entity_type="stock", entity_ids=[1]),
                      DateRange(start="2024-07-01", end="2024-07-02", frequency="daily"))


def _stub_check(monkeypatch, answers):
    """answers: command -> status. Anything unlisted resolves."""
    import fd_open_data_mcp.crawl.planner as planner

    monkeypatch.setattr(
        planner, "check_command",
        lambda source, command: (answers.get(command, cap.RESOLVABLE), "stub"))


@pytest.fixture
def enforce_capability(monkeypatch):
    monkeypatch.setenv("FD_CAPABILITY_CHECK", "enforce")


def test_report_mode_keeps_an_unresolvable_candidate(session, monkeypatch):
    """Report-only until asked: a mis-set switch must not narrow a live crawl."""
    monkeypatch.delenv("FD_CAPABILITY_CHECK", raising=False)
    _stub_check(monkeypatch, {"option_czce_hist": cap.UNRESOLVABLE})
    concept = _seed_candidates(session, ["stock_zh_a_hist", "option_czce_hist"])

    plan = _plan(session, concept.id)

    commands = [ps.function_command for ps in plan.wanted_concepts[0].ranked_sources]
    assert commands == ["stock_zh_a_hist", "option_czce_hist"]
    assert plan.suppressed == []


def test_enforce_mode_excludes_an_unresolvable_candidate(
        session, monkeypatch, enforce_capability):
    _stub_check(monkeypatch, {"option_czce_hist": cap.UNRESOLVABLE})
    concept = _seed_candidates(session, ["stock_zh_a_hist", "option_czce_hist"])

    plan = _plan(session, concept.id)

    commands = [ps.function_command for ps in plan.wanted_concepts[0].ranked_sources]
    assert commands == ["stock_zh_a_hist"]


def test_the_exclusion_is_reported_as_unresolvable(
        session, monkeypatch, enforce_capability):
    _stub_check(monkeypatch, {"option_czce_hist": cap.UNRESOLVABLE})
    concept = _seed_candidates(session, ["stock_zh_a_hist", "option_czce_hist"])

    plan = _plan(session, concept.id)

    assert [(s["function_id"], s["reason"]) for s in plan.suppressed] == [
        (plan.suppressed[0]["function_id"], "endpoint unresolvable")]
    assert plan.suppressed[0]["command"] == "option_czce_hist"
    assert plan.suppressed[0]["concept_id"] == concept.id


def test_enforce_mode_keeps_an_unverifiable_candidate(
        session, monkeypatch, enforce_capability):
    """A host without the crawl libraries cannot judge — and must not exclude.
    Treating 'I don't know' as 'it's broken' would drop working datapaths."""
    _stub_check(monkeypatch, {"stock_zh_a_hist": cap.UNVERIFIABLE})
    concept = _seed_candidates(session, ["stock_zh_a_hist"])

    plan = _plan(session, concept.id)

    assert len(plan.wanted_concepts[0].ranked_sources) == 1
    assert plan.suppressed == []


def test_a_resolvable_candidate_is_never_excluded(
        session, monkeypatch, enforce_capability):
    _stub_check(monkeypatch, {})
    concept = _seed_candidates(session, ["stock_zh_a_hist", "stock_zh_a_daily"])

    plan = _plan(session, concept.id)

    assert len(plan.wanted_concepts[0].ranked_sources) == 2
    assert plan.suppressed == []


def test_enforcement_applies_before_the_chain_bound(
        session, monkeypatch, enforce_capability):
    """Same reasoning as suppression: excluding after truncation would leave the
    bound filled with candidates that cannot run."""
    monkeypatch.setenv("FD_CRAWL_CHAIN_MAX", "1")
    _stub_check(monkeypatch, {"stock_zh_a_hist": cap.UNRESOLVABLE})
    concept = _seed_candidates(session, ["stock_zh_a_hist", "stock_zh_a_daily"])

    plan = _plan(session, concept.id)

    commands = [ps.function_command for ps in plan.wanted_concepts[0].ranked_sources]
    assert commands == ["stock_zh_a_daily"]

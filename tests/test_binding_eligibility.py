"""Binding eligibility: entity-domain agreement and the confirmation gate.

Two holes let concept `price.close`/`stock` reach 117 dispatch-eligible
bindings (two of which accounted for every fetch the fleet made on
2026-09-16):

1. Rules matched on column NAME alone, so every akshare endpoint with a
   `收盘`/`close` column bound to "stock closing price" — options, futures,
   bonds, funds and indices among them.
2. The dispatch gate was satisfiable by confidence alone, and the rule table
   emits 0.85-0.9, so the review step gated nothing: all 1,033 machine
   proposals were live.

The tightened gate narrows live candidates, so it ships report-only by default.
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.models import (
    Concept, ConceptBinding, Function, FunctionColumn, Source,
)
from fd_open_data_mcp.semantic import bindings
from fd_open_data_mcp.semantic.mapper_llm import function_domain, propose_concept


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setenv(bindings.ELIGIBILITY_GATE_ENV, "enforce")


# --- 3.1 entity-domain agreement --------------------------------------------

@pytest.mark.parametrize("command", [
    "option_czce_hist",      # the incident's smaller culprit
    "futures_zh_spot",
    "bond_zh_hs_daily",
    "fund_lof_hist_em",
    "index_zh_a_hist",
])
def test_a_cross_domain_rule_proposal_is_refused(command):
    """The column is a close price; the function is not a stock."""
    assert propose_concept("今收盘", command=command) is None
    assert propose_concept("close", command=command) is None


def test_a_same_domain_rule_proposal_is_accepted():
    prop = propose_concept("收盘", command="stock_zh_a_hist")
    assert prop is not None
    assert prop["code"] == "price.close"
    assert prop["entity_type"] == "stock"


def test_an_undeclared_domain_is_not_checked():
    """yfinance's `ticker_*` declares no domain — its Close IS a stock close, so
    declaring `company` here would refuse a correct binding."""
    assert function_domain("ticker_history") is None
    prop = propose_concept("Close", command="ticker_history")
    assert prop is not None
    assert prop["code"] == "price.close"


def test_document_hints_are_never_domain_checked():
    prop = propose_concept("标题", semantic_type="title",
                           command="mee_tzgg_archive")
    assert prop is not None
    assert prop["code"] == "doc.title"


def test_the_domain_exception_table_beats_the_prefix():
    """akshare names index endpoints with a `stock_` prefix."""
    assert function_domain("stock_zh_a_hist") == "stock"
    assert function_domain("stock_zh_index_daily") == "index"
    assert propose_concept("收盘", command="stock_zh_index_daily") is None


@pytest.mark.parametrize("command,expected", [
    ("option_czce_hist", "future"),
    ("futures_zh_spot", "future"),
    ("bond_gb_us_sina", "bond"),
    ("fund_etf_hist_em", "fund"),
    ("index_hist_sw", "index"),
    ("stock_us_hist", "stock"),
    ("get_indicator_data", None),
    (None, None),
])
def test_function_domain_reads_the_command(command, expected):
    assert function_domain(command) == expected


@pytest.mark.parametrize("command,source,expected", [
    # the command declares nothing; the source does
    ("get_indicator_data", "wbgapi", "country"),
    ("mee_tzgg_archive", "cn-gov", "industry"),
    ("ticker_history", "yfinance", "stock"),
    ("company_financials", "edgar", "stock"),
    # the command's declaration wins over the source's
    ("option_czce_hist", "akshare", "future"),
    # neither declares one
    ("some_command", "no-such-source", None),
    ("some_command", None, None),
])
def test_function_domain_falls_back_to_the_source(command, source, expected):
    """A source that only serves one entity domain is positive evidence, and
    without it the 103 prefix-less bindings (wbgapi macro, cn-gov documents)
    would empty nine concepts."""
    assert function_domain(command, source) == expected


def test_a_prefixless_source_domain_binding_is_accepted():
    """`get_indicator_data` -> gdp/country is correct and must stay confirmable."""
    prop = propose_concept("NY.GDP.MKTP.CD", command="get_indicator_data",
                           source="wbgapi")
    assert prop is not None
    assert prop["entity_type"] == "country"


def test_a_source_domain_still_refuses_a_real_mismatch():
    """The fallback must not become a blanket pass: a cn-gov document column
    proposed for a stock concept is still refused."""
    assert propose_concept("收盘", command="mee_tzgg_archive", source="cn-gov") is None


# --- fixtures for the gate tests --------------------------------------------

def _seed_bindings(session, specs):
    """specs: (command, column, provenance, reviewed, confidence) -> Concept.

    All binds to the same concept (price.close / stock), which is the shape that
    made the incident possible.
    """
    source = Source(name="akshare", label="akshare")
    session.add(source)
    session.flush()
    concept = Concept(code="price.close", entity_type="stock", measure="",
                      unit="currency", frequency="daily")
    session.add(concept)
    session.flush()
    for i, (command, column, provenance, reviewed, confidence) in enumerate(specs):
        fn = Function(source_id=source.id, command=command)
        session.add(fn)
        session.flush()
        col = FunctionColumn(function_id=fn.id, name=column)
        session.add(col)
        session.flush()
        session.add(ConceptBinding(
            concept_id=concept.id, column_id=col.id, confidence=confidence,
            provenance=provenance, reviewed=reviewed))
    session.commit()
    return concept


# --- 3.2 the confirmation gate ----------------------------------------------

def test_a_high_confidence_unreviewed_proposal_is_withheld(session, enforce):
    """0.9 confidence used to be sufficient on its own. It is not any more."""
    concept = _seed_bindings(session, [
        ("stock_zh_a_hist", "收盘", "llm", False, 0.9),   # machine, unconfirmed
        ("stock_zh_a_daily", "收盘", "manual", True, 0.9),
    ])
    got = bindings.dispatch_candidates(session, concept.id)
    assert [b.provenance for b in got] == ["manual"]


def test_a_withheld_proposal_lands_in_the_review_queue(session, enforce):
    _seed_bindings(session, [("stock_zh_a_hist", "收盘", "llm", False, 0.9)])
    queue = bindings.review_queue(session)
    assert len(queue) == 1
    assert queue[0].confidence == 0.9


def test_confirmation_admits_the_binding(session, enforce):
    _seed_bindings(session, [("stock_zh_a_hist", "收盘", "llm", False, 0.9)])
    binding = session.query(ConceptBinding).one()
    bindings.confirm_binding(session, binding.id)
    assert len(bindings.dispatch_candidates(session, binding.concept_id)) == 1


def test_sample_confirmation_admits_the_binding(session, enforce):
    concept = _seed_bindings(session, [("stock_zh_a_hist", "收盘", "llm", False, 0.9)])
    fn_id = session.query(Function).one().id
    bindings.promote_on_sample(session, fn_id, [("收盘", "float")])
    assert len(bindings.dispatch_candidates(session, concept.id)) == 1


def test_a_manual_binding_is_admitted_regardless_of_confidence(session, enforce):
    """The gate tightens the machine-proposal path; a human confirmation was
    never scored and stays that way."""
    concept = _seed_bindings(session, [
        ("stock_zh_a_hist", "收盘", "manual", True, 0.2),
    ])
    assert len(bindings.dispatch_candidates(session, concept.id)) == 1


def test_a_low_confidence_machine_proposal_is_withheld_in_both_modes(session):
    """The pre-existing threshold is untouched by the gate."""
    concept = _seed_bindings(session, [
        ("stock_zh_a_hist", "收盘", "llm", False, 0.2),
    ])
    assert bindings.dispatch_candidates(session, concept.id) == []


# --- 3.3 report-only default -------------------------------------------------

def test_report_mode_returns_exactly_the_pre_change_set(session):
    """With the gate unreported, dispatch behavior is unchanged — a mis-written
    gate must not be able to narrow a live crawl by itself."""
    concept = _seed_bindings(session, [
        ("stock_zh_a_hist", "收盘", "llm", False, 0.9),
        ("stock_zh_a_daily", "收盘", "manual", True, 0.9),
        ("stock_zh_a_minute", "收盘", "llm", False, 0.2),
        ("stock_zh_a_hist_tx", "收盘", "sample-confirmed", True, 0.5),
    ])
    assert bindings.gate_mode() == "report"
    rows = session.query(ConceptBinding).filter_by(concept_id=concept.id).all()
    expected = [b.id for b in rows
                if b.provenance in ("manual", "sample-confirmed")
                or b.confidence >= 0.6]
    assert [b.id for b in bindings.dispatch_candidates(session, concept.id)] == expected


def test_the_env_var_selects_the_mode(monkeypatch):
    monkeypatch.delenv(bindings.ELIGIBILITY_GATE_ENV, raising=False)
    assert bindings.gate_mode() == "report"
    monkeypatch.setenv(bindings.ELIGIBILITY_GATE_ENV, "enforce")
    assert bindings.gate_mode() == "enforce"
    monkeypatch.setenv(bindings.ELIGIBILITY_GATE_ENV, "nonsense")
    assert bindings.gate_mode() == "report"   # never fails open into enforcing


# --- 3.4 the report ----------------------------------------------------------

def test_eligibility_report_states_the_delta(session):
    concept = _seed_bindings(session, [
        ("stock_zh_a_hist", "收盘", "llm", False, 0.9),      # withheld
        ("stock_zh_a_daily", "收盘", "manual", True, 0.9),   # kept
        ("stock_zh_a_minute", "收盘", "llm", False, 0.2),    # withheld (already was)
    ])
    report = bindings.eligibility_report(session)

    assert report["gate_mode"] == "report"
    assert report["candidates_before"][concept.id] == 2   # manual + 0.9 llm
    assert report["candidates_after"][concept.id] == 1    # manual only
    assert report["bindings_withheld"] == 1
    assert report["concepts_losing_candidates"] == [concept.id]
    assert report["concepts_left_with_none"] == []
    row = report["withheld"][0]
    assert row["command"] == "stock_zh_a_hist"
    assert row["concept_code"] == "price.close"


def test_eligibility_report_flags_a_concept_left_with_nothing(session):
    concept = _seed_bindings(session, [
        ("stock_zh_a_hist", "收盘", "llm", False, 0.9),
    ])
    report = bindings.eligibility_report(session)
    assert report["concepts_left_with_none"] == [concept.id]


def test_eligibility_report_writes_nothing(session):
    _seed_bindings(session, [("stock_zh_a_hist", "收盘", "llm", False, 0.9)])
    before = session.query(ConceptBinding).count()
    bindings.eligibility_report(session)
    assert session.query(ConceptBinding).count() == before
    assert session.query(ConceptBinding).one().reviewed is False


# --- 4.1 rule-based confirmation --------------------------------------------

@pytest.fixture
def resolvable(monkeypatch):
    from fd_open_data_mcp.fetch import capability
    monkeypatch.setattr(
        capability, "check_command",
        lambda source, command: (capability.RESOLVABLE, "test"))


@pytest.fixture
def unverifiable(monkeypatch):
    from fd_open_data_mcp.fetch import capability
    monkeypatch.setattr(
        capability, "check_command",
        lambda source, command: (capability.UNVERIFIABLE, "no library here"))


def test_rule_confirms_a_domain_agreeing_resolvable_binding(session, resolvable):
    _seed_bindings(session, [("stock_zh_a_hist", "收盘", "llm", False, 0.9)])
    result = bindings.confirm_by_rule(session, dry_run=False)
    assert result["confirmed"] == 1
    binding = session.query(ConceptBinding).one()
    assert binding.reviewed is True
    assert binding.provenance == bindings.CONFIRMED_BY_RULE


def test_rule_refuses_a_cross_domain_binding(session, resolvable):
    """Options bound to a stock price is the defect, not a candidate."""
    _seed_bindings(session, [("option_czce_hist", "今收盘", "llm", False, 0.9)])
    result = bindings.confirm_by_rule(session, dry_run=False)
    assert result["confirmed"] == 0
    assert result["cross_domain"] == 1
    assert session.query(ConceptBinding).one().provenance == "llm"


def test_rule_refuses_an_unverifiable_endpoint_and_names_the_source(
        session, unverifiable):
    """A host without the crawl libraries must not read as 'nothing is
    supported' — it is reported as its own outcome."""
    _seed_bindings(session, [("stock_zh_a_hist", "收盘", "llm", False, 0.9)])
    result = bindings.confirm_by_rule(session, dry_run=False)
    assert result["confirmed"] == 0
    assert result["unverifiable"] == 1
    assert result["unverifiable_sources"] == ["akshare"]


def test_rule_refuses_an_unresolvable_endpoint(session, monkeypatch):
    from fd_open_data_mcp.fetch import capability
    monkeypatch.setattr(
        capability, "check_command",
        lambda source, command: (capability.UNRESOLVABLE, "gone"))
    _seed_bindings(session, [("stock_zh_a_hist", "收盘", "llm", False, 0.9)])
    result = bindings.confirm_by_rule(session, dry_run=False)
    assert result["unresolvable"] == 1
    assert result["confirmed"] == 0


def test_rule_leaves_an_undeclared_domain_for_review(session, resolvable):
    """An undeclared domain is the absence of a contradiction, not evidence."""
    _seed_bindings(session, [("ticker_history", "Close", "llm", False, 0.9)])
    result = bindings.confirm_by_rule(session, dry_run=False)
    assert result["no_declared_domain"] == 1
    assert result["confirmed"] == 0


def test_rule_is_a_no_op_by_default(session, resolvable):
    _seed_bindings(session, [("stock_zh_a_hist", "收盘", "llm", False, 0.9)])
    result = bindings.confirm_by_rule(session)
    assert result["dry_run"] is True
    assert result["confirmed"] == 1
    binding = session.query(ConceptBinding).one()
    assert binding.provenance == "llm"
    assert binding.reviewed is False


def test_rule_confirmed_binding_becomes_dispatchable_under_the_gate(
        session, resolvable, enforce):
    concept = _seed_bindings(session, [
        ("stock_zh_a_hist", "收盘", "llm", False, 0.9)])
    assert bindings.dispatch_candidates(session, concept.id) == []
    bindings.confirm_by_rule(session, dry_run=False)
    assert len(bindings.dispatch_candidates(session, concept.id)) == 1


def test_rule_splits_a_mixed_batch(session, resolvable):
    """The real-world shape: a stock function and a broken stock function are
    confirmed; options and futures bound to a stock price are withheld."""
    _seed_bindings(session, [
        ("stock_zh_a_hist", "收盘", "llm", False, 0.9),
        ("stock_zh_a_minute", "收盘", "llm", False, 0.9),
        ("option_czce_hist", "今收盘", "llm", False, 0.9),
        ("futures_zh_spot", "close", "llm", False, 0.9),
    ])
    result = bindings.confirm_by_rule(session, dry_run=False)
    assert result["confirmed"] == 2
    assert result["cross_domain"] == 2
    assert session.query(ConceptBinding).filter_by(
        provenance=bindings.CONFIRMED_BY_RULE).count() == 2


# --- function verification: eligibility is the SECOND gate -------------------
#
# `_bindings_for_source` filters `fn.verified`, so a confirmed binding on an
# unverified function is still unreachable. Measured on the canonical store,
# 508 of the 536 rule-confirmed bindings sat on `verified=false` functions —
# ~95% of the confirmation pass was inert, and concept price.close/stock had 40
# stock_* functions bound, all unverified.

def _seed_function(session, command, verified, concepts):
    """One function bound to one column per (concept_code, entity_type)."""
    source = session.query(Source).filter_by(name="akshare").first()
    if source is None:
        source = Source(name="akshare", label="akshare")
        session.add(source)
        session.flush()
    fn = Function(source_id=source.id, command=command, verified=verified)
    session.add(fn)
    session.flush()
    for i, (code, entity_type) in enumerate(concepts):
        col = FunctionColumn(function_id=fn.id, name=f"col{i}-{command}")
        session.add(col)
        session.flush()
        concept = session.query(Concept).filter_by(
            code=code, entity_type=entity_type, measure="", unit="currency",
            frequency="daily").first()
        if concept is None:
            concept = Concept(code=code, entity_type=entity_type, measure="",
                              unit="currency", frequency="daily")
            session.add(concept)
            session.flush()
        session.add(ConceptBinding(concept_id=concept.id, column_id=col.id,
                                   confidence=0.9, provenance="manual", reviewed=True))
    session.commit()
    return fn


@pytest.fixture
def resolvable_capability(monkeypatch):
    from fd_open_data_mcp.fetch import capability
    monkeypatch.setattr(capability, "check_command",
                        lambda source, command: (capability.RESOLVABLE, "stub"))


def test_a_domain_consistent_resolvable_function_is_verifiable(session, resolvable_capability):
    _seed_function(session, "stock_zh_a_hist", False, [("price.close", "stock")])
    result = bindings.verify_functions_by_rule(session)

    assert result["verifiable"] == 1
    assert [f["command"] for f in result["functions"]] == ["stock_zh_a_hist"]


def test_a_cross_domain_function_is_refused(session, resolvable_capability):
    """A fund endpoint bound to a stock concept is exactly the incident's defect
    and must never be verified."""
    _seed_function(session, "fund_lof_hist_em", False, [("price.close", "stock")])
    result = bindings.verify_functions_by_rule(session)

    assert result["verifiable"] == 0
    assert result["domain_inconsistent"] == 1


def test_a_function_bound_outside_its_domain_anywhere_is_refused(session, resolvable_capability):
    """Domain consistency is judged across EVERY concept the function serves, not
    just the first — one bad binding is enough to refuse."""
    _seed_function(session, "stock_zh_a_hist", False,
                   [("price.close", "stock"), ("gdp", "country")])
    result = bindings.verify_functions_by_rule(session)

    assert result["verifiable"] == 0
    assert result["domain_inconsistent"] == 1


def test_a_function_bound_to_several_agreeing_concepts_is_verifiable(session, resolvable_capability):
    _seed_function(session, "stock_zh_a_hist", False,
                   [("price.close", "stock"), ("price.open", "stock")])
    assert bindings.verify_functions_by_rule(session)["verifiable"] == 1


def test_an_unresolvable_function_is_refused(session, monkeypatch):
    from fd_open_data_mcp.fetch import capability
    monkeypatch.setattr(capability, "check_command",
                        lambda source, command: (capability.UNRESOLVABLE, "gone"))
    _seed_function(session, "stock_zh_a_tick_tx", False, [("price.close", "stock")])
    result = bindings.verify_functions_by_rule(session)

    assert result["verifiable"] == 0
    assert result["unresolvable"] == 1


def test_a_function_whose_environment_cannot_judge_it_is_refused(session, monkeypatch):
    """A host without the crawl libraries must not verify everything."""
    from fd_open_data_mcp.fetch import capability
    monkeypatch.setattr(capability, "check_command",
                        lambda source, command: (capability.UNVERIFIABLE, "no library"))
    _seed_function(session, "stock_zh_a_hist", False, [("price.close", "stock")])
    result = bindings.verify_functions_by_rule(session)

    assert result["verifiable"] == 0
    assert result["unverifiable"] == 1


def test_a_function_declaring_no_domain_is_left_alone(session, resolvable_capability):
    """No declared domain means no positive evidence."""
    _seed_function(session, "get_history", False, [("price.close", "stock")])
    result = bindings.verify_functions_by_rule(session)

    assert result["verifiable"] == 0
    assert result["no_declared_domain"] == 1


def test_an_already_verified_function_is_not_reconsidered(session, resolvable_capability):
    _seed_function(session, "stock_zh_a_hist", True, [("price.close", "stock")])
    assert bindings.verify_functions_by_rule(session)["considered"] == 0


def test_dry_run_writes_nothing(session, resolvable_capability):
    _seed_function(session, "stock_zh_a_hist", False, [("price.close", "stock")])
    result = bindings.verify_functions_by_rule(session)

    assert result["dry_run"] is True
    assert result["verifiable"] == 1
    assert session.query(Function).one().verified is False


def test_applying_marks_the_function_verified(session, resolvable_capability):
    _seed_function(session, "stock_zh_a_hist", False, [("price.close", "stock")])
    bindings.verify_functions_by_rule(session, dry_run=False)

    assert session.query(Function).one().verified is True


def test_a_command_naming_another_instrument_overrides_the_source_default():
    """yfinance serves both stock prices and option chains, so the source default
    is too coarse: `ticker_option_chain`'s `volume` is the contract's, not the
    underlying's traded volume. Same failure shape as the incident."""
    assert function_domain("ticker_option_chain", "yfinance") == "future"
    assert function_domain("ticker_history", "yfinance") == "stock"
    assert propose_concept("volume", command="ticker_option_chain",
                           source="yfinance") is None

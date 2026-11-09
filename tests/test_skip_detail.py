"""A skipped run must say why (2026-11-09: four policies spun at 0 yield with detail NULL)."""
from fd_open_data_mcp.models import PolicyRun
from fd_open_data_mcp.refresh.reconciler import (
    _all_refused_detail, _skip_detail, classify_yield,
)


def _run(**kw):
    r = PolicyRun(policy_id=1)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_empty_plan_is_no_op_and_explains_itself():
    run = _run(plan_cells=0, rows_attempted=None, rows_new=None)
    run.status = classify_yield(run)
    assert run.status == "no_op"
    detail = _skip_detail(run)
    assert detail and "empty" in detail and "no_op" in detail


def test_fully_deduped_run_is_redundant_and_explains_itself():
    run = _run(plan_cells=10, rows_attempted=10, rows_new=0)
    run.status = classify_yield(run)
    assert run.status == "redundant"
    detail = _skip_detail(run)
    assert detail and "already stored" in detail


def test_success_and_zero_yield_have_no_skip_detail():
    ok = _run(plan_cells=5, rows_attempted=5, rows_new=5)
    ok.status = classify_yield(ok)
    assert _skip_detail(ok) is None

    zy = _run(plan_cells=5, rows_attempted=0, rows_new=0)
    zy.status = classify_yield(zy)
    assert zy.status == "zero_yield"
    assert _skip_detail(zy) is None


# ─── an all-refused plan must not be read as "nothing due" ───────────────────
# 2026-09-17/19: four enabled policies had every pinned concept flagged
# deprecated. Each round planned 0 cells, launched a job, and closed as no_op.
# 54 days of silence, because zero cells had only one recorded meaning.

def _unroutable(*pairs):
    return [
        {"concept_id": cid, "code": code, "reason": "concept is deprecated",
         "canonical_replacement": None, "canonical_replacement_id": None}
        for cid, code in pairs
    ]


def test_all_concepts_refused_is_a_refusal_not_a_no_op():
    detail = _all_refused_detail([383, 384], _unroutable((383, "price.open"), (384, "price.close")))
    assert detail is not None
    assert detail.startswith("refused: every requested concept is unroutable")
    assert "price.open: concept is deprecated" in detail


def test_partial_refusal_still_plans_the_survivors():
    # policy 1 real shape: two live concepts + two deprecated ones
    detail = _all_refused_detail(
        [378, 379, 384, 381], _unroutable((384, "price.close"), (381, "yield.7day_annualized")),
    )
    assert detail is None


def test_empty_plan_without_unroutable_stays_a_no_op():
    # watermark current: nothing filtered, nothing refused -> healthy no_op
    assert _all_refused_detail([18, 19], []) is None


def test_long_refusal_list_is_truncated_but_counted():
    ids = list(range(1, 6))
    detail = _all_refused_detail(ids, _unroutable(*[(i, f"c{i}") for i in ids]))
    assert detail is not None and "(+2 more)" in detail
    assert detail.count("concept is deprecated") == 3

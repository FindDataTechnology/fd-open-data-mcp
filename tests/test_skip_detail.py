"""A skipped run must say why (2026-11-09: four policies spun at 0 yield with detail NULL)."""
from fd_open_data_mcp.models import PolicyRun
from fd_open_data_mcp.refresh.reconciler import _skip_detail, classify_yield


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

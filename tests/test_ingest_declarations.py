"""接入声明巡检（define-ingest-revision-and-coverage 任务 3.1–3.3）。

两条要害：
① 「未声明」与「声明值非法」是两件事——拼错的字段等于没有声明，但必须单独点名。
② 「声明存在」不等于通过：一致性核验必须打到读面上，否则又在造 `eventlog.set_status`。
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fd_open_data_mcp.ingest_declarations import (
    COVERAGE_CAPABILITIES,
    REVISION_POLICIES,
    declaration_from_manifest,
    glob_manifests,
    inspect,
    load_declarations,
    probe_coverage_consistency,
)


def _manifest(tmp_path: Path, name: str, extra: str = "") -> Path:
    d = tmp_path / name
    d.mkdir()
    p = d / "manifest.yaml"
    p.write_text(textwrap.dedent(f"""
        version: "2"
        name: {name}
        label: {name} label
        functions:
          - command: run_{name.replace('-', '_')}
    """) + textwrap.dedent(extra), encoding="utf-8")
    return p


def test_undeclared_is_pending_not_invalid(tmp_path):
    """没写字段 = 待补；不是错误，进待办清单。"""
    p = _manifest(tmp_path, "src-a")
    decl = declaration_from_manifest(p)
    assert decl.missing_fields == ["revision_policy", "coverage_capability"]
    assert decl.invalid == {}
    rep = inspect([decl])
    # finalize_window 是 2026-11-10 定型窗口那轮新增的第三个待补轴：
    # 它不进 missing_fields（不是必填声明），但巡检要单独计数。
    assert rep["counts"] == {"declared": 0, "pending": 1, "invalid": 0,
                             "pending_finalize_window": 1}


def test_declared_source_passes(tmp_path):
    p = _manifest(tmp_path, "src-b",
                  "revision_policy: batch_revision\ncoverage_capability: full\n")
    decl = declaration_from_manifest(p)
    assert decl.declared is True
    assert inspect([decl])["counts"]["declared"] == 1


def test_misspelled_value_is_invalid_not_declared(tmp_path):
    """拼错即等于没声明，但必须单独点名——否则会被当成"已声明"混过去。"""
    p = _manifest(tmp_path, "src-c",
                  "revision_policy: overwrite-with-history\ncoverage_capability: FULL\n")
    decl = declaration_from_manifest(p)
    assert decl.declared is False
    assert set(decl.invalid) == {"revision_policy", "coverage_capability"}
    rep = inspect([decl])
    assert rep["counts"]["invalid"] == 1
    assert rep["counts"]["declared"] == 0


def test_partial_declaration_is_pending(tmp_path):
    p = _manifest(tmp_path, "src-d", "revision_policy: immutable\n")
    decl = declaration_from_manifest(p)
    assert decl.missing_fields == ["coverage_capability"]
    assert inspect([decl])["counts"]["pending"] == 1


def test_glob_finds_spider_layout(tmp_path):
    _manifest(tmp_path, "one")
    _manifest(tmp_path, "two")
    assert len(glob_manifests(tmp_path)) == 2
    assert [d.name for d in load_declarations(glob_manifests(tmp_path))] == ["one", "two"]


def test_json_is_machine_readable(tmp_path):
    reps = inspect(load_declarations([_manifest(tmp_path, "src-e")]))
    import json
    json.dumps(reps)  # 不抛即通过


# ---------------- 声明与行为一致性（打到读面上） ----------------

def _read_fn(coverage=None, degraded=None):
    def _f(concept_id, entity_type, entity_id, start, end):
        got = {}
        if coverage is not None:
            got["coverage"] = coverage
        if degraded is not None:
            got["degraded"] = degraded
        return got
    return _f


def test_probe_flags_read_surface_without_declarations():
    """读面不返回 coverage/degraded → 声明无从谈起。"""
    res = probe_coverage_consistency(_read_fn(), concept_id=1, entity_type="stock",
                                     entity_id=2, declared_capability="full")
    assert res["consistent"] is False
    assert len(res["problems"]) == 2


def test_probe_catches_lying_full_declaration():
    """声明 full 却在必然为空的窗口返回 missing=[] → 撒谎。"""
    read = _read_fn(coverage={"requested": ["1900-01-01", "1900-01-05"],
                              "covered": [], "missing": []},
                    degraded={"degraded": False})
    res = probe_coverage_consistency(read, concept_id=1, entity_type="stock",
                                     entity_id=2, declared_capability="full")
    assert res["consistent"] is False
    assert any("不一致" in p for p in res["problems"])


def test_probe_accepts_honest_full_declaration():
    read = _read_fn(coverage={"requested": ["1900-01-01", "1900-01-05"],
                              "covered": [], "missing": [["1900-01-01", "1900-01-05"]]},
                    degraded={"degraded": False})
    res = probe_coverage_consistency(read, concept_id=1, entity_type="stock",
                                     entity_id=2, declared_capability="full")
    assert res["consistent"] is True


def test_probe_skips_coverage_claim_for_none():
    """声明 none 的源不承诺覆盖，空窗口 missing 为空不算撒谎。"""
    read = _read_fn(coverage={"requested": ["1900-01-01", "1900-01-05"],
                              "covered": [], "missing": []},
                    degraded={"degraded": False})
    res = probe_coverage_consistency(read, concept_id=1, entity_type="stock",
                                     entity_id=2, declared_capability="none")
    assert res["consistent"] is True


def test_allowed_sets_are_stable():
    """允许集合是契约的一部分：改它必须同时改 spec。"""
    assert REVISION_POLICIES == ("overwrite_with_history", "batch_revision", "immutable")
    assert COVERAGE_CAPABILITIES == ("full", "gaps_only", "none")


# ---------------- 存量分桶（只建议，不写回） ----------------

from fd_open_data_mcp.ingest_declarations import classify_manifest, classify_paths


def test_price_concepts_default_to_point_revision_not_batch():
    """价格类不等于前复权：默认建议逐点留痕，并把"是否整段重写"标为待确认。

    2026-11-09 逐桶复核纠正：本仓 14 个价格源全是大宗/CPI，修订逐点，
    原先一律建议 batch_revision 是错的。
    """
    info = classify_manifest({
        "name": "px", "data_period": "day",
        "concepts": [{"concept": "price.close"}],
    })
    assert info["bucket"] == "price_like"
    assert info["suggested"]["revision_policy"] == "overwrite_with_history"
    assert info["review_forward_adjustment"] is True


def test_period_series_gets_overwrite_with_history():
    info = classify_manifest({"name": "macro", "data_period": "month",
                              "concepts": [{"concept": "gdp.total"}]})
    assert info["bucket"] == "period_series"
    assert info["suggested"]["revision_policy"] == "overwrite_with_history"


def test_no_signal_is_undetermined_and_gets_no_suggestion():
    """两个信号都没有 → 不给建议，必须人工读。猜一个就是批量填假声明。"""
    info = classify_manifest({"name": "mystery"})
    assert info["bucket"] == "undetermined"
    assert info["suggested"] == {}
    assert info["needs_human_read"] is True


def test_classification_never_suggests_full():
    """`full` 只能由读面探针证明，绝不由分桶推断。"""
    for data in (
        {"name": "a", "data_period": "day", "concepts": [{"concept": "price.close"}]},
        {"name": "b", "data_period": "month"},
        {"name": "c"},
    ):
        sug = classify_manifest(data)["suggested"]
        assert sug.get("coverage_capability") != "full"


def test_classify_paths_counts_match_inputs(tmp_path):
    _manifest(tmp_path, "px1",
              "data_period: day\nconcepts:\n  - concept: price.close\n")
    _manifest(tmp_path, "macro1", "data_period: month\n")
    _manifest(tmp_path, "mystery1")
    cls = classify_paths(glob_manifests(tmp_path))
    assert cls["total"] == 3
    assert cls["counts"]["price_like"] == 1
    assert cls["counts"]["period_series"] == 1
    assert cls["counts"]["undetermined"] == 1

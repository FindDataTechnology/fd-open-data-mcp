"""声明与行为对拍、以及定型窗口的待补清单（任务 5.1–5.3、2.4）。

核心纪律：**没跑出问题 ≠ 证明没问题**。数据上无法反证的声明必须单列，不许混进
"一致"里冒充绿灯。
"""

from datetime import datetime

import pytest

from fd_open_data_mcp.ingest_declarations import (
    SourceDeclaration,
    inspect,
    probe_revision_policy_consistency,
)
from fd_open_data_mcp.models import Concept, ObservationRevision, SemanticObservation


@pytest.fixture
def concept(session):
    c = Concept(code="probe.decl", entity_type="country",
                frequency="yearly", unit="%", verified=True)
    session.add(c)
    session.flush()
    return c


def add_revision(session, concept, source):
    session.add(ObservationRevision(
        concept_id=concept.id, entity_type="country", entity_id=1,
        date="2020-12-31", granularity="year", source_used=source,
        revision_kind="overwrite", old_value="1", new_value="2",
        revised_at=datetime(2024, 6, 1),
    ))
    session.commit()


# --- 5.1 immutable 源被覆写 → 报红 -----------------------------------------

def test_immutable_source_with_revisions_is_a_violation(session, concept):
    add_revision(session, concept, "strict_immutable")
    decls = [SourceDeclaration(name="strict_immutable", path="x",
                               revision_policy="immutable")]

    out = probe_revision_policy_consistency(
        session, ["strict_immutable"], decls)

    assert out["consistent"] is False
    assert out["violations"][0]["kind"] == "behaviour_missing"
    assert out["violations"][0]["revision_rows"] == 1


def test_immutable_source_without_revisions_is_not_proof(session, concept):
    """没有修订记录 = 未被反证，不等于被证明。必须单列，不得算作通过。"""
    decls = [SourceDeclaration(name="clean_immutable", path="x",
                               revision_policy="immutable")]

    out = probe_revision_policy_consistency(session, ["clean_immutable"], decls)

    assert out["consistent"] is True
    assert out["not_contradicted_but_unproven"] == ["clean_immutable"]
    assert out["verified_by_invariant"] == []


# --- 5.2 留痕缺失与声明缺失分开归类 ----------------------------------------

def test_undeclared_is_its_own_bucket(session, concept):
    decls = [SourceDeclaration(name="no_policy", path="x")]

    out = probe_revision_policy_consistency(session, ["no_policy"], decls)

    assert out["undeclared"] == ["no_policy"]
    assert out["violations"] == []


def test_history_policy_is_marked_invariant_not_data_verified(session, concept):
    add_revision(session, concept, "history_src")
    decls = [SourceDeclaration(name="history_src", path="x",
                               revision_policy="overwrite_with_history")]

    out = probe_revision_policy_consistency(session, ["history_src"], decls)

    assert out["violations"] == []
    entry = out["verified_by_invariant"][0]
    assert entry["verified_by"] == "invariant_test"
    assert "非数据核验" in out["note"]


# --- 2.4 未声明定型窗口进待补清单 ------------------------------------------

def test_undeclared_finalize_window_goes_to_its_own_pending_list():
    decls = [
        SourceDeclaration(name="a", path="a", revision_policy="overwrite_with_history",
                          coverage_capability="full", finalize_grace_days=365),
        SourceDeclaration(name="b", path="b", revision_policy="overwrite_with_history",
                          coverage_capability="full"),
    ]

    out = inspect(decls)

    assert [d["name"] for d in out["pending_finalize_window"]] == ["b"]
    assert out["counts"]["pending_finalize_window"] == 1
    # 既有两个字段的进度不受影响
    assert out["counts"]["declared"] == 2
    assert out["counts"]["pending"] == 0


def test_negativate_grace_declaration_is_invalid_not_defaulted(tmp_path):
    from fd_open_data_mcp.ingest_declarations import declaration_from_manifest

    m = tmp_path / "manifest.yaml"
    m.write_text(
        "name: bad_window\n"
        "revision_policy: overwrite_with_history\n"
        "finalize_grace_days: -5\n",
        encoding="utf-8",
    )

    decl = declaration_from_manifest(m)

    assert decl.finalize_grace_days is None
    assert "finalize_grace_days" in decl.invalid

"""Pure-logic unit tests for scripts/verify_vector_dual_read.py.

The script itself needs a PostgreSQL database with pgvector (the <=> operator),
so these tests cover the pieces that must not depend on one: the ranking
comparison functions, the embedding payload parser, path A's brute-force
ranking, and the CLI's environment guards.
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_vector_dual_read.py"


@pytest.fixture(scope="module")
def vdr():
    spec = importlib.util.spec_from_file_location("verify_vector_dual_read", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# jaccard
# ---------------------------------------------------------------------------

class TestJaccard:
    def test_identical_lists_score_one(self, vdr):
        assert vdr.jaccard([1, 2, 3], [1, 2, 3]) == 1.0

    def test_disjoint_sets_score_zero(self, vdr):
        assert vdr.jaccard([1, 2], [3, 4]) == 0.0

    def test_two_of_six_union_elements_shared(self, vdr):
        # {3,4} shared over a 6-element union -> 1/3
        assert vdr.jaccard([1, 2, 3, 4], [3, 4, 5, 6]) == pytest.approx(1 / 3)

    def test_empty_vs_empty_is_full_agreement(self, vdr):
        assert vdr.jaccard([], []) == 1.0

    def test_empty_vs_nonempty_is_zero(self, vdr):
        assert vdr.jaccard([], [1]) == 0.0

    def test_order_is_irrelevant(self, vdr):
        assert vdr.jaccard([1, 2, 3], [3, 2, 1]) == 1.0


# ---------------------------------------------------------------------------
# kendall_tau
# ---------------------------------------------------------------------------

class TestKendallTau:
    def test_identical_ordering_is_one(self, vdr):
        assert vdr.kendall_tau([10, 20, 30], [10, 20, 30]) == 1.0

    def test_reversed_ordering_is_minus_one(self, vdr):
        assert vdr.kendall_tau([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0

    def test_single_swap_of_adjacent_pair(self, vdr):
        # of 6 pairs exactly one is inverted -> (5-1)/6
        assert vdr.kendall_tau([1, 2, 3, 4], [2, 1, 3, 4]) == pytest.approx(4 / 6)

    def test_two_swaps(self, vdr):
        # pairs (1,2) and (3,4) inverted -> (4-2)/6 = 1/3
        assert vdr.kendall_tau([1, 2, 3, 4], [2, 1, 4, 3]) == pytest.approx(1 / 3)

    def test_computed_on_common_items_only(self, vdr):
        # items 5 and 6 are unique to one side each; the shared part
        # [1,2,3] vs [3,2,1] is fully inverted
        assert vdr.kendall_tau([1, 2, 3, 5], [3, 2, 1, 6]) == pytest.approx(-1.0)

    def test_fewer_than_two_common_items_defined_as_one(self, vdr):
        assert vdr.kendall_tau([1], [1]) == 1.0
        assert vdr.kendall_tau([1, 2], [3, 4]) == 1.0
        assert vdr.kendall_tau([], []) == 1.0

    def test_known_five_element_case(self, vdr):
        # A=[a,b,c,d,e]; B swaps every adjacent pair: [b,a,d,c,e]
        # inverted pairs: (a,b),(c,d) -> 8 concordant, 2 discordant -> 6/10
        a = ["a", "b", "c", "d", "e"]
        b = ["b", "a", "d", "c", "e"]
        assert vdr.kendall_tau(a, b) == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# parse_embedding — every storage form the two tables use
# ---------------------------------------------------------------------------

class TestParseEmbedding:
    def test_psycopg2_jsonb_arrives_as_list(self, vdr):
        assert vdr.parse_embedding([0.1, 0.2, 0.3]) == [0.1, 0.2, 0.3]

    def test_entity_text_column_json_string(self, vdr):
        assert vdr.parse_embedding("[1.0, 2.0]") == [1.0, 2.0]

    def test_pgvector_text_form_without_brackets(self, vdr):
        assert vdr.parse_embedding("0.5,-0.25,1.0") == [0.5, -0.25, 1.0]

    def test_single_element_vector(self, vdr):
        assert vdr.parse_embedding("[3.5]") == [3.5]
        assert vdr.parse_embedding("3.5") == [3.5]

    def test_bytes_payload(self, vdr):
        assert vdr.parse_embedding(b"[0.1, 0.2]") == [0.1, 0.2]

    def test_none_and_empty(self, vdr):
        assert vdr.parse_embedding(None) == []
        assert vdr.parse_embedding("") == []

    def test_integers_become_floats(self, vdr):
        out = vdr.parse_embedding("[1, 2]")
        assert out == [1.0, 2.0]
        assert all(isinstance(x, float) for x in out)

    def test_unrecognised_type_raises(self, vdr):
        with pytest.raises(TypeError):
            vdr.parse_embedding(42)


# ---------------------------------------------------------------------------
# cosine + path A ranking
# ---------------------------------------------------------------------------

class TestCosineAndRanking:
    def test_identical_vectors_similarity_one(self, vdr):
        v = [0.1, -0.2, 0.3]
        assert vdr.cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_similarity_zero(self, vdr):
        assert vdr.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_similarity_minus_one(self, vdr):
        assert vdr.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_zero_vector_is_similarity_zero(self, vdr):
        assert vdr.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_rank_python_orders_by_similarity(self, vdr):
        query = [1.0, 0.0]
        ids = [1, 2, 3]
        vectors = [[0.0, 1.0], [1.0, 0.0], [0.7, 0.7]]  # sims 0, 1, ~0.707
        assert vdr.rank_python(ids, vectors, query, 3) == [2, 3, 1]

    def test_rank_python_respects_topk(self, vdr):
        ids = [1, 2, 3]
        vectors = [[1.0, 0.0]] * 3
        assert vdr.rank_python(ids, vectors, [1.0, 0.0], 2) == [1, 2]

    def test_rank_python_breaks_ties_by_id(self, vdr):
        ids = [7, 3, 5]
        vectors = [[1.0, 0.0]] * 3
        assert vdr.rank_python(ids, vectors, [1.0, 0.0], 3) == [3, 5, 7]

    def test_vector_literal_is_valid_json_and_pgvector_input(self, vdr):
        lit = vdr.vector_literal([0.5, -0.25])
        assert lit == "[0.5, -0.25]"
        assert vdr.parse_embedding(lit) == [0.5, -0.25]


# ---------------------------------------------------------------------------
# compare_topk — the per-sample verdict assembler
# ---------------------------------------------------------------------------

class TestCompareTopk:
    def test_identical_lists(self, vdr):
        out = vdr.compare_topk([1, 2, 3], [1, 2, 3])
        assert out["jaccard"] == 1.0 and out["tau"] == 1.0
        assert out["only_json"] == [] and out["only_vector"] == []

    def test_same_set_reordered(self, vdr):
        out = vdr.compare_topk([1, 2, 3, 4], [2, 1, 4, 3])
        assert out["jaccard"] == 1.0
        assert out["tau"] == pytest.approx(1 / 3)
        assert out["only_json"] == [] and out["only_vector"] == []

    def test_different_sets(self, vdr):
        out = vdr.compare_topk([1, 2, 3, 4], [3, 4, 5, 6])
        assert out["jaccard"] == pytest.approx(1 / 3)
        assert out["only_json"] == [1, 2]
        assert out["only_vector"] == [5, 6]


# ---------------------------------------------------------------------------
# CLI environment guards
# ---------------------------------------------------------------------------

class TestCliGuards:
    def test_missing_env_var_exits_with_message(self, vdr, monkeypatch):
        monkeypatch.delenv("FD_OPEN_DATA_MCP_DATABASE_URL", raising=False)
        with pytest.raises(SystemExit, match="FD_OPEN_DATA_MCP_DATABASE_URL"):
            vdr.main([])

    def test_sqlite_url_is_refused(self, vdr, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path}/x.db"
        )
        with pytest.raises(SystemExit, match="PostgreSQL"):
            vdr.main([])


def test_script_syntax_is_valid_python():
    # the acceptance check itself, kept as a test
    import ast

    ast.parse(SCRIPT.read_text())


def test_script_has_no_write_statements(vdr):
    """The verifier is read-only: no INSERT/UPDATE/DELETE/DDL anywhere."""
    import re

    src = SCRIPT.read_text()
    statements = re.findall(r'f?"((?:[^"\\]|\\.)*)"', src)
    mutating = re.compile(
        r"\b(insert|update|delete|create|drop|alter|truncate)\b", re.I
    )
    offenders = [s for s in statements if mutating.search(s)]
    assert offenders == [], f"read-only script contains mutating SQL: {offenders}"
    assert math.isclose(vdr.cosine_similarity([1.0], [1.0]), 1.0)

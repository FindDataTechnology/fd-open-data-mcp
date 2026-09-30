"""Backend chain + alias resolution for the shared embedding facade
(openspec image-slimming: fastembed first, sentence-transformers fallback,
vector contract unchanged)."""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from fd_open_data_mcp.embeddings import model as emb_model
from fd_open_data_mcp.embeddings.fastembed_backend import (
    FastembedEncoder,
    resolve_model_id,
)


@pytest.fixture(autouse=True)
def _reset_models():
    emb_model.reset_model()
    yield
    emb_model.reset_model()


def test_alias_resolution():
    assert (
        resolve_model_id("all-MiniLM-L6-v2")
        == "sentence-transformers/all-MiniLM-L6-v2"
    )
    # Namespaced ids and unknown models pass through untouched.
    assert (
        resolve_model_id("sentence-transformers/all-MiniLM-L6-v2")
        == "sentence-transformers/all-MiniLM-L6-v2"
    )
    assert resolve_model_id("BAAI/bge-small-en-v1.5") == "BAAI/bge-small-en-v1.5"


def test_get_model_falls_back_to_sentence_transformers(monkeypatch):
    """A model outside fastembed's catalog loads via the legacy backend."""
    calls = []

    def fake_fastembed(name):
        calls.append(("fastembed", name))
        raise ValueError(f"Model {name} is not supported in TextEmbedding.")

    class FakeST:
        def __init__(self, name):
            calls.append(("st", name))

    monkeypatch.setattr(emb_model, "_load_fastembed", fake_fastembed)
    monkeypatch.setattr(
        emb_model, "_load_sentence_transformers", lambda name: FakeST(name)
    )

    model = emb_model.get_model("exotic-model")
    assert isinstance(model, FakeST)
    assert calls == [("fastembed", "exotic-model"), ("st", "exotic-model")]


def test_get_model_prefers_fastembed(monkeypatch):
    calls = []

    class FakeFastembed:
        def __init__(self, name):
            calls.append(("fastembed", name))

    monkeypatch.setattr(
        emb_model, "_load_fastembed", lambda name: FakeFastembed(name)
    )
    monkeypatch.setattr(
        emb_model,
        "_load_sentence_transformers",
        lambda name: pytest.fail("legacy backend must not load when fastembed works"),
    )

    assert isinstance(emb_model.get_model("all-MiniLM-L6-v2"), FakeFastembed)
    assert calls == [("fastembed", "all-MiniLM-L6-v2")]


def test_error_names_model(monkeypatch):
    """Both backends failing surfaces the model name, not an opaque error."""

    def boom(name):
        raise RuntimeError("no network")

    monkeypatch.setattr(emb_model, "_load_fastembed", boom)
    monkeypatch.setattr(emb_model, "_load_sentence_transformers", boom)

    with pytest.raises(RuntimeError) as exc:
        emb_model.get_model("some-model-x")
    assert "some-model-x" in str(exc.value)
    assert "FD_MCP_EMBEDDING_MODEL" in str(exc.value)


def test_encoder_str_and_list_semantics(monkeypatch):
    """encode(str) -> 1-D, encode(list) -> 2-D, mirroring SentenceTransformer."""

    class FakeTextEmbedding:
        def __init__(self, **kwargs):
            assert (
                kwargs.get("model_name")
                == "sentence-transformers/all-MiniLM-L6-v2"
            ), kwargs

        def embed(self, seq):
            for i, _ in enumerate(seq):
                yield np.full(4, float(i), dtype=np.float32)

    fake_module = types.ModuleType("fastembed")
    fake_module.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake_module)

    encoder = FastembedEncoder("all-MiniLM-L6-v2")
    single = encoder.encode("hello")
    batch = encoder.encode(["hello", "world"], show_progress_bar=True)
    assert single.shape == (4,)
    assert batch.shape == (2, 4)
    assert single.tolist() == [0.0, 0.0, 0.0, 0.0]


@pytest.mark.network
def test_real_backend_dims():
    """Real fastembed run: the default model embeds to 384 dims (downloads)."""
    model = emb_model.get_model()
    out = model.encode(["semantic search smoke"])
    assert out.shape == (1, 384)

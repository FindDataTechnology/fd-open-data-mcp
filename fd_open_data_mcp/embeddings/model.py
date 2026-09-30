"""Shared embedding-model resolution for every search path.

Resolves the embedding model by model name, never a machine-specific
cache path (spec semantic-search: portable model resolution). The runtime
image bakes the model into the default HF cache under HF_HUB_OFFLINE=1 and
dev machines resolve it from their own cache; ``FD_MCP_EMBEDDING_MODEL``
overrides for exotic setups.

Backend chain (openspec image-slimming): fastembed/onnxruntime first — the
same model weights on a ~15MB runtime instead of torch's ~195MB — falling
back to sentence-transformers for environments or models that only exist
there (install the ``search-legacy`` extra).
"""
from __future__ import annotations

import os
from typing import Any

MODEL_NAME = os.environ.get("FD_MCP_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

_MODELS: dict[str, Any] = {}


def _load_fastembed(model_name: str):
    from fd_open_data_mcp.embeddings.fastembed_backend import FastembedEncoder
    return FastembedEncoder(model_name)


def _load_sentence_transformers(model_name: str):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(model_name)


def _load_model(model_name: str):
    fast_error: Exception | None = None
    try:
        return _load_fastembed(model_name)
    except ImportError:
        pass  # fastembed not installed — legacy-only environment
    except Exception as e:  # model outside fastembed's catalog, load failure, ...
        fast_error = e
    try:
        return _load_sentence_transformers(model_name)
    except ImportError as e:
        raise RuntimeError(
            f"Failed to load embedding model '{model_name}': no usable backend "
            f"(install fd-open-data-mcp[search] for fastembed/onnxruntime, or "
            f"[search-legacy] for sentence-transformers)"
            + (f"; fastembed error: {fast_error}" if fast_error else "")
        ) from e
    except Exception as e:
        detail = f"; sentence-transformers error: {e}"
        if fast_error is not None:
            detail += f"; fastembed error: {fast_error}"
        raise RuntimeError(
            f"Failed to load embedding model '{model_name}'{detail} "
            f"(set FD_MCP_EMBEDDING_MODEL to override)"
        ) from e


def get_model(model_name: str | None = None):
    """Load the embedding model once per process (lazy singleton per name)."""
    name = model_name or MODEL_NAME
    if name not in _MODELS:
        _MODELS[name] = _load_model(name)
    return _MODELS[name]


def reset_model() -> None:
    """Drop loaded models (tests / model swap)."""
    _MODELS.clear()

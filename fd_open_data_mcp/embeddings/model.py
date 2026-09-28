"""Shared embedding-model resolution for every search path.

Resolves the sentence-transformer by model name, never a machine-specific
cache path (spec semantic-search: portable model resolution). The runtime
image bakes the model into the default HF cache under HF_HUB_OFFLINE=1 and
dev machines resolve it from their own cache; ``FD_MCP_EMBEDDING_MODEL``
overrides for exotic setups.
"""
from __future__ import annotations

import os

MODEL_NAME = os.environ.get("FD_MCP_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

_MODEL = None


def get_model():
    """Load the embedding model once per process (lazy singleton)."""
    global _MODEL
    if _MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
            _MODEL = SentenceTransformer(MODEL_NAME)
        except Exception as e:  # surface the model name, not a raw FS error
            raise RuntimeError(
                f"Failed to load embedding model '{MODEL_NAME}' "
                f"(set FD_MCP_EMBEDDING_MODEL to override): {e}"
            ) from e
    return _MODEL


def reset_model() -> None:
    """Drop the loaded model (tests / model swap)."""
    global _MODEL
    _MODEL = None

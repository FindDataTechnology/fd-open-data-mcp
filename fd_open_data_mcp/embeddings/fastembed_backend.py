"""fastembed (onnxruntime) backend for the shared embedding facade.

Same weights as the sentence-transformers reference — fastembed serves the
official ONNX exports (sentence-transformers/all-MiniLM-L6-v2) on
onnxruntime, trading torch's ~195MB compressed runtime for ~15MB. Vector
compatibility is gated by the equivalence check in openspec image-slimming
(cosine > 0.999 on sampled inputs, same dims); query time uses the
scale-invariant cosine operator, so fastembed's unit-length outputs coexist
with the unnormalized vectors already stored in pgvector.

Model files resolve through huggingface_hub's standard cache layout
(models--<org>--<name> under HF_HOME), so a cache baked at build time under
HF_HUB_OFFLINE=1 resolves at runtime exactly as the sentence-transformers
bake did.
"""
from __future__ import annotations

import os
from typing import Any, List

import numpy as np

# Bare sentence-transformers aliases -> fastembed's namespaced model ids.
_ALIASES = {
    "all-MiniLM-L6-v2": "sentence-transformers/all-MiniLM-L6-v2",
}


def resolve_model_id(model_name: str) -> str:
    """Map a bare model alias to fastembed's namespaced id (pass-through otherwise)."""
    return _ALIASES.get(model_name, model_name)


class FastembedEncoder:
    """Duck-typed SentenceTransformer stand-in served by onnxruntime.

    Mirrors the subset of ``SentenceTransformer`` the codebase relies on:
    ``encode(str) -> 1-D array`` and ``encode(list[str]) -> 2-D array``.
    ``show_progress_bar`` is accepted and ignored (fastembed streams
    internally). Output normalization stays at fastembed's default (unit
    length), which the cosine operator is invariant to.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from fastembed import TextEmbedding  # lazy: pulls in onnxruntime

        self.model_name = model_name
        self.resolved_id = resolve_model_id(model_name)
        self._dim: int | None = None
        # fastembed hands its cache_dir to huggingface_hub's snapshot_download,
        # where an explicit cache_dir OVERRIDES HF_HOME — a model baked under
        # HF_HOME/hub is invisible to it offline. FD_MCP_FASTEMBED_CACHE pins
        # one explicit dir holding the hub layout directly (what the image
        # bakes); unset = fastembed's own defaults (networked dev machines).
        cache_dir = os.environ.get("FD_MCP_FASTEMBED_CACHE") or None
        # HF_HUB_OFFLINE=1 must short-circuit fastembed's model_info()/
        # list_repo_tree() metadata calls, not just the file downloads.
        self._model = TextEmbedding(
            model_name=self.resolved_id,
            cache_dir=cache_dir,
            local_files_only=os.environ.get("HF_HUB_OFFLINE", "") == "1",
        )

    def get_embedding_dimension(self) -> int:
        """SentenceTransformer-compatible dimension probe (cached)."""
        if self._dim is None:
            self._dim = int(self.encode(["dimension probe"]).shape[1])
        return self._dim

    def encode(self, texts: Any, show_progress_bar: bool = False, **_: Any) -> np.ndarray:
        single = isinstance(texts, str)
        seq: List[str] = [texts] if single else list(texts)
        embeddings = np.asarray(list(self._model.embed(seq)), dtype=np.float32)
        return embeddings[0] if single else embeddings

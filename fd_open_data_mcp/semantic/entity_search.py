"""Entity semantic search using vector embeddings."""
from __future__ import annotations

import logging
import os
from typing import List, Dict, Any, Optional

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fd_open_data_mcp import vector_backend

logger = logging.getLogger(__name__)


def _embedding_cache_size() -> int:
    return int(os.environ.get("EMBEDDING_CACHE_SIZE", "1000"))


class EntitySemanticSearch:
    """Search entities using semantic similarity with vector embeddings.

    Held as a process-wide singleton via ``fd_open_data_mcp.engines`` so the
    query-embedding cache (EMBEDDING_CACHE_SIZE wired) survives across calls.
    """

    def __init__(self, database_url: str, model_name: str = "all-MiniLM-L6-v2"):
        """
        Initialize semantic search.

        Args:
            database_url: Database connection URL
            model_name: Name of the embedding model to use
        """
        self.database_url = database_url
        self.model_name = model_name
        self.engine = create_engine(database_url)
        self.Session = sessionmaker(bind=self.engine)

        # Load embedding model
        try:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(model_name)
            logger.info(f"Loaded embedding model: {model_name}")
        except ImportError:
            raise ImportError(
                "sentence-transformers is required. Install with: "
                "pip install sentence-transformers"
            )

        # Embedding cache: query_text -> embedding_vector
        self._embedding_cache: Dict[str, List[float]] = {}
        self._cache_hits = 0
        self._cache_misses = 0

        # Warm up cache on startup
        self._warm_up_cache()

    def _warm_up_cache(self) -> None:
        """Warm up embedding cache with common queries."""
        common_queries = [
            "inflation", "GDP", "unemployment", "interest rate",
            "country", "stock", "company", "industry",
            "technology", "finance", "healthcare", "energy"
        ]

        logger.info(f"Warming up embedding cache with {len(common_queries)} common queries...")
        for query in common_queries:
            if query not in self._embedding_cache:
                self._embedding_cache[query] = self.model.encode(query).tolist()

        logger.info(f"Cache warmed up with {len(self._embedding_cache)} embeddings")

    def _get_embedding(self, text: str) -> List[float]:
        """Get embedding for text, using cache if available."""
        if text in self._embedding_cache:
            self._cache_hits += 1
            return self._embedding_cache[text]

        self._cache_misses += 1
        embedding = self.model.encode(text).tolist()
        self._embedding_cache[text] = embedding

        # Bound the cache (EMBEDDING_CACHE_SIZE, default 1000): evict the
        # oldest ~10% so a busy server doesn't grow without limit.
        limit = _embedding_cache_size()
        if len(self._embedding_cache) > limit:
            keys_to_remove = list(self._embedding_cache.keys())[: max(1, limit // 10)]
            for key in keys_to_remove:
                del self._embedding_cache[key]

        return embedding

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._cache_hits + self._cache_misses
        hit_rate = self._cache_hits / total if total > 0 else 0.0

        return {
            "cache_size": len(self._embedding_cache),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate": hit_rate
        }

    def invalidate_cache(self) -> None:
        """Clear the embedding cache."""
        self._embedding_cache.clear()
        self._cache_hits = 0
        self._cache_misses = 0
        logger.info("Embedding cache invalidated")

    def search(
        self,
        query: str,
        entity_type: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Search entities using semantic similarity.

        Args:
            query: Search query text
            entity_type: Optional filter by entity type
            limit: Maximum number of results to return

        Returns:
            List of entity dictionaries with similarity scores
        """
        # Generate query embedding (with caching)
        query_embedding = self._get_embedding(query)

        session = self.Session()
        try:
            # Candidate retrieval goes through the vector backend abstraction
            # (FD_MCP_VECTOR_BACKEND: json | matrix | pgvector). The default
            # json backend is the legacy full-scan path, behavior-for-behavior.
            return vector_backend.search_entity_candidates(
                session,
                query_embedding,
                entity_type=entity_type,
                limit=limit,
                model=self.model_name,
            )

        finally:
            session.close()

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        a = np.array(vec1)
        b = np.array(vec2)

        # Handle zero vectors
        if np.linalg.norm(a) == 0 or np.linalg.norm(b) == 0:
            return 0.0

        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    def search_unified(
        self,
        query: str,
        entity_type: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Unified search across both entities and concepts.

        Args:
            query: Search query text
            entity_type: Optional filter by entity type (for entities only)
            limit: Maximum number of results to return

        Returns:
            List of results (entities and concepts) with similarity scores
        """
        # Search entities
        entity_results = self.search(query, entity_type, limit)

        # Add type marker
        for result in entity_results:
            result["result_type"] = "entity"

        # Search concepts (reuse existing semantic_search)
        from fd_open_data_mcp.semantic_search import semantic_search as concept_search

        concept_results = concept_search(query, entity_type=None, limit=limit)

        # Add type marker
        for result in concept_results:
            result["result_type"] = "concept"

        # Merge and sort by similarity
        all_results = entity_results + concept_results
        all_results.sort(key=lambda x: x["similarity"], reverse=True)

        return all_results[:limit]

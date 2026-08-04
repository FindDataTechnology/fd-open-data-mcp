"""
Unit tests for NetworkX graph search and semantic search functionality.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import json
import numpy as np

from fd_open_data_mcp.graph.manager import EntityGraphManager
from fd_open_data_mcp.embeddings.generator import EntityEmbeddingGenerator
from fd_open_data_mcp.semantic.entity_search import EntitySemanticSearch


class TestEntityGraphManager:
    """Tests for EntityGraphManager."""

    @pytest.fixture
    def graph_manager(self):
        """Create graph manager with test database."""
        # Use SQLite in-memory database for testing
        manager = EntityGraphManager(
            database_url='sqlite:///:memory:',
            cache_ttl=300
        )
        return manager

    def test_load_graph_from_database(self, graph_manager):
        """Test loading graph from database."""
        # This test requires actual database setup, skip for now
        # In production, this would test the actual graph loading
        pytest.skip("Requires database setup")

    def test_bfs_traversal(self, graph_manager):
        """Test BFS traversal."""
        # Create a simple test graph directly without loading from DB
        import networkx as nx
        graph_manager._graph = nx.Graph()
        graph_manager._graph.add_edges_from([(1, 2), (2, 3), (3, 4)])

        graph_manager._node_metadata = {
            1: {'id': 1, 'entity_type': 'country', 'code': 'CN', 'name_en': 'China', 'name_zh': '中国'},
            2: {'id': 2, 'entity_type': 'country', 'code': 'US', 'name_en': 'United States', 'name_zh': '美国'},
            3: {'id': 3, 'entity_type': 'country', 'code': 'JP', 'name_en': 'Japan', 'name_zh': '日本'},
            4: {'id': 4, 'entity_type': 'country', 'code': 'KR', 'name_en': 'South Korea', 'name_zh': '韩国'},
        }

        # Perform BFS from node 1
        result = graph_manager.bfs_traversal(1, max_depth=2)

        # Verify results
        assert len(result) > 0
        assert result[0]['code'] == 'CN'

    def test_shortest_path(self, graph_manager):
        """Test shortest path algorithm."""
        import networkx as nx
        graph_manager._graph = nx.Graph()
        graph_manager._graph.add_edges_from([(1, 2), (2, 3), (3, 4), (1, 4)])

        graph_manager._node_metadata = {
            1: {'id': 1, 'entity_type': 'country', 'code': 'CN', 'name_en': 'China', 'name_zh': '中国'},
            2: {'id': 2, 'entity_type': 'country', 'code': 'US', 'name_en': 'United States', 'name_zh': '美国'},
            3: {'id': 3, 'entity_type': 'country', 'code': 'JP', 'name_en': 'Japan', 'name_zh': '日本'},
            4: {'id': 4, 'entity_type': 'country', 'code': 'KR', 'name_en': 'South Korea', 'name_zh': '韩国'},
        }

        # Find shortest path
        result = graph_manager.shortest_path(1, 4)

        # Verify path exists
        assert len(result) > 0
        assert result[0]['code'] == 'CN'
        assert result[-1]['code'] == 'KR'


class TestEntityEmbeddingGenerator:
    """Tests for EntityEmbeddingGenerator."""

    def test_generate_embedding(self):
        """Test generating embedding for text."""
        # Use real model for this test
        generator = EntityEmbeddingGenerator(model_name='all-MiniLM-L6-v2')
        embedding = generator.generate_embedding("test text")

        assert isinstance(embedding, list)
        assert len(embedding) > 0

    def test_prepare_entity_text(self):
        """Test preparing entity text for embedding."""
        generator = EntityEmbeddingGenerator(model_name='all-MiniLM-L6-v2')
        entity = {
            'entity_type': 'country',
            'code': 'CN',
            'name_en': 'China',
            'name_zh': '中国',
            'metadata_json': '{"region": "Asia"}'
        }

        text = generator.prepare_entity_text(entity)

        assert 'country' in text
        assert 'China' in text
        assert '中国' in text
        assert 'CN' in text


class TestEntitySemanticSearch:
    """Tests for EntitySemanticSearch."""

    def test_cosine_similarity(self):
        """Test cosine similarity calculation."""
        search = EntitySemanticSearch(database_url='sqlite:///:memory:')
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [1.0, 0.0, 0.0]

        similarity = search._cosine_similarity(vec1, vec2)

        assert similarity == 1.0

    def test_cosine_similarity_orthogonal(self):
        """Test cosine similarity for orthogonal vectors."""
        search = EntitySemanticSearch(database_url='sqlite:///:memory:')
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [0.0, 1.0, 0.0]

        similarity = search._cosine_similarity(vec1, vec2)

        assert similarity == 0.0

    def test_cache_hit(self):
        """Test embedding cache hit."""
        search = EntitySemanticSearch(database_url='sqlite:///:memory:')

        # First call should be a cache miss
        embedding1 = search._get_embedding("test")
        assert search._cache_misses == 1
        assert search._cache_hits == 0

        # Second call should be a cache hit
        embedding2 = search._get_embedding("test")
        assert search._cache_hits == 1
        assert search._cache_misses == 1

        # Embeddings should be the same
        assert embedding1 == embedding2

    def test_cache_invalidation(self):
        """Test cache invalidation."""
        search = EntitySemanticSearch(database_url='sqlite:///:memory:')

        # Add something to cache
        search._get_embedding("test")
        assert len(search._embedding_cache) > 0

        # Invalidate cache
        search.invalidate_cache()
        assert len(search._embedding_cache) == 0
        assert search._cache_hits == 0
        assert search._cache_misses == 0


class TestIntegration:
    """Integration tests for the complete system."""

    def test_graph_search_mcp_tool(self):
        """Test graph_search MCP tool."""
        from fd_open_data_mcp.server import graph_search

        # This would require a real database, so we'll just verify the tool exists
        assert callable(graph_search)

    def test_semantic_search_entities_mcp_tool(self):
        """Test semantic_search_entities MCP tool."""
        from fd_open_data_mcp.server import semantic_search_entities

        # Verify the tool exists
        assert callable(semantic_search_entities)

    def test_semantic_search_unified_mcp_tool(self):
        """Test semantic_search_unified MCP tool."""
        from fd_open_data_mcp.server import semantic_search_unified

        # Verify the tool exists
        assert callable(semantic_search_unified)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

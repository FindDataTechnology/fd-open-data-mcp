"""
Performance benchmarks for graph search and semantic search.
"""
import time
import pytest
from fd_open_data_mcp.graph.manager import EntityGraphManager
from fd_open_data_mcp.semantic.entity_search import EntitySemanticSearch
from fd_open_data_mcp.embeddings.generator import EntityEmbeddingGenerator


class TestGraphPerformance:
    """Performance tests for graph operations."""

    @pytest.fixture
    def graph_manager(self):
        """Create graph manager with real database."""
        import os
        database_url = os.environ.get(
            "FD_OPEN_DATA_MCP_DATABASE_URL",
            "postgresql://admin:admin123@192.168.1.4:5433/postgres"
        )
        return EntityGraphManager(database_url=database_url, cache_ttl=300)

    def test_graph_loading_performance(self, graph_manager):
        """Test graph loading performance."""
        start = time.time()
        graph = graph_manager.get_graph(force_reload=True)
        load_time = time.time() - start

        print(f"\nGraph Loading Performance:")
        print(f"  Nodes: {graph.number_of_nodes()}")
        print(f"  Edges: {graph.number_of_edges()}")
        print(f"  Load Time: {load_time:.2f}s")
        print(f"  Nodes/sec: {graph.number_of_nodes() / load_time:.0f}")

        # Should load in reasonable time
        assert load_time < 5.0, f"Graph loading too slow: {load_time:.2f}s"

    def test_bfs_traversal_performance(self, graph_manager):
        """Test BFS traversal performance."""
        graph = graph_manager.get_graph()

        # Get a starting node
        nodes = list(graph.nodes())
        if not nodes:
            pytest.skip("No nodes in graph")

        start_node = nodes[0]

        # Run BFS multiple times
        times = []
        for _ in range(10):
            start = time.time()
            result = graph_manager.bfs_traversal(start_node, max_depth=3)
            times.append(time.time() - start)

        avg_time = sum(times) / len(times)
        max_time = max(times)

        print(f"\nBFS Traversal Performance:")
        print(f"  Average Time: {avg_time*1000:.2f}ms")
        print(f"  Max Time: {max_time*1000:.2f}ms")
        print(f"  Results: {len(result)} entities")

        # Should be very fast
        assert avg_time < 0.1, f"BFS too slow: {avg_time*1000:.2f}ms"

    def test_shortest_path_performance(self, graph_manager):
        """Test shortest path performance."""
        graph = graph_manager.get_graph()

        # Get two nodes
        nodes = list(graph.nodes())
        if len(nodes) < 2:
            pytest.skip("Not enough nodes in graph")

        start_node = nodes[0]
        end_node = nodes[-1]

        # Run shortest path multiple times
        times = []
        for _ in range(10):
            start = time.time()
            result = graph_manager.shortest_path(start_node, end_node)
            times.append(time.time() - start)

        avg_time = sum(times) / len(times)
        max_time = max(times)

        print(f"\nShortest Path Performance:")
        print(f"  Average Time: {avg_time*1000:.2f}ms")
        print(f"  Max Time: {max_time*1000:.2f}ms")
        print(f"  Path Length: {len(result)} entities")

        # Should be fast
        assert avg_time < 0.5, f"Shortest path too slow: {avg_time*1000:.2f}ms"

    def test_cache_performance(self, graph_manager):
        """Test cache performance."""
        # First call (cache miss)
        start = time.time()
        graph1 = graph_manager.get_graph()
        first_time = time.time() - start

        # Second call (cache hit)
        start = time.time()
        graph2 = graph_manager.get_graph()
        second_time = time.time() - start

        print(f"\nCache Performance:")
        print(f"  First Call (miss): {first_time*1000:.2f}ms")
        print(f"  Second Call (hit): {second_time*1000:.2f}ms")
        print(f"  Speedup: {first_time / second_time:.1f}x")

        # Cache hit should be much faster
        assert second_time < first_time / 10, "Cache not effective"


class TestSemanticSearchPerformance:
    """Performance tests for semantic search."""

    @pytest.fixture
    def semantic_search(self):
        """Create semantic search with real database."""
        import os
        database_url = os.environ.get(
            "FD_OPEN_DATA_MCP_DATABASE_URL",
            "postgresql://admin:admin123@192.168.1.4:5433/postgres"
        )
        return EntitySemanticSearch(database_url=database_url)

    def test_embedding_generation_performance(self, semantic_search):
        """Test embedding generation performance."""
        queries = [
            "inflation",
            "GDP growth",
            "technology companies",
            "Asian countries",
            "financial indicators"
        ]

        times = []
        for query in queries:
            start = time.time()
            embedding = semantic_search._get_embedding(query)
            times.append(time.time() - start)

        avg_time = sum(times) / len(times)

        print(f"\nEmbedding Generation Performance:")
        print(f"  Average Time: {avg_time*1000:.2f}ms")
        print(f"  Embedding Dimension: {len(embedding)}")

        # Should be fast
        assert avg_time < 0.1, f"Embedding generation too slow: {avg_time*1000:.2f}ms"

    def test_search_performance(self, semantic_search):
        """Test search performance."""
        queries = [
            "inflation indicators",
            "technology companies",
            "Asian countries",
            "GDP growth",
            "financial data"
        ]

        times = []
        for query in queries:
            start = time.time()
            results = semantic_search.search(query, limit=20)
            times.append(time.time() - start)

        avg_time = sum(times) / len(times)
        max_time = max(times)

        print(f"\nSearch Performance:")
        print(f"  Average Time: {avg_time*1000:.2f}ms")
        print(f"  Max Time: {max_time*1000:.2f}ms")
        print(f"  Average Results: {len(results)} entities")

        # Should be reasonably fast
        assert avg_time < 1.0, f"Search too slow: {avg_time*1000:.2f}ms"

    def test_cache_hit_rate(self, semantic_search):
        """Test cache hit rate."""
        # Clear cache
        semantic_search.invalidate_cache()

        # Run same queries multiple times
        queries = ["inflation", "GDP", "technology"] * 5

        for query in queries:
            semantic_search._get_embedding(query)

        stats = semantic_search.get_cache_stats()

        print(f"\nCache Performance:")
        print(f"  Cache Size: {stats['cache_size']}")
        print(f"  Hits: {stats['cache_hits']}")
        print(f"  Misses: {stats['cache_misses']}")
        print(f"  Hit Rate: {stats['hit_rate']:.2%}")

        # Should have good hit rate
        assert stats['hit_rate'] > 0.5, f"Cache hit rate too low: {stats['hit_rate']:.2%}"


class TestEmbeddingGeneratorPerformance:
    """Performance tests for embedding generator."""

    @pytest.fixture
    def embedding_generator(self):
        """Create embedding generator."""
        return EntityEmbeddingGenerator(model_name="all-MiniLM-L6-v2")

    def test_batch_embedding_performance(self, embedding_generator):
        """Test batch embedding generation performance."""
        texts = [f"Test text {i}" for i in range(100)]

        start = time.time()
        embeddings = embedding_generator.generate_embeddings_batch(texts)
        total_time = time.time() - start

        avg_time = total_time / len(texts)

        print(f"\nBatch Embedding Performance:")
        print(f"  Total Time: {total_time:.2f}s")
        print(f"  Average per Text: {avg_time*1000:.2f}ms")
        print(f"  Texts/sec: {len(texts) / total_time:.0f}")
        print(f"  Embeddings Generated: {len(embeddings)}")

        # Should be reasonably fast
        assert total_time < 10.0, f"Batch embedding too slow: {total_time:.2f}s"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])

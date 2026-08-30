"""
Entity Graph Manager using NetworkX.

Provides graph-based entity relationship queries with BFS, DFS, shortest path,
and subgraph extraction capabilities.
"""
import time
import logging
from typing import Optional, List, Dict, Any, Set
from collections import deque

import networkx as nx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)


class EntityGraphManager:
    """
    Manages entity graph using NetworkX.

    Loads entities and relationships from database, caches the graph in memory,
    and provides graph traversal and query operations.
    """

    def __init__(self, database_url: str, cache_ttl: int = 300):
        """
        Initialize the graph manager.

        Args:
            database_url: Database connection URL
            cache_ttl: Cache time-to-live in seconds (default: 5 minutes)
        """
        self.database_url = database_url
        self.cache_ttl = cache_ttl
        self.engine = create_engine(database_url)
        self.Session = sessionmaker(bind=self.engine)

        # Graph cache
        self._graph: Optional[nx.Graph] = None
        self._last_load: Optional[float] = None
        self._node_metadata: Dict[int, Dict[str, Any]] = {}

        logger.info(f"Initialized EntityGraphManager with cache_ttl={cache_ttl}s")

    def get_graph(self, force_reload: bool = False) -> nx.Graph:
        """
        Get the entity graph, loading from database if necessary.

        Args:
            force_reload: Force reload from database even if cache is valid

        Returns:
            NetworkX Graph object
        """
        current_time = time.time()

        # Check if cache is valid. A _graph with no _last_load was injected
        # (tests / programmatic seeding), not loaded from the DB — honor it
        # as-is; only DB loads expire against the TTL.
        if (not force_reload and
            self._graph is not None and
            (self._last_load is None or
             current_time - self._last_load < self.cache_ttl)):
            return self._graph

        # Load graph from database
        logger.info("Loading graph from database...")
        start_time = time.time()

        self._graph = nx.Graph()
        self._node_metadata = {}

        session = self.Session()
        try:
            # Load entities (nodes)
            result = session.execute(text("""
                SELECT id, entity_type, code, name_en, name_zh, metadata_json
                FROM entities
            """))

            for row in result:
                node_id = row.id
                self._graph.add_node(node_id)
                self._node_metadata[node_id] = {
                    'id': node_id,
                    'entity_type': row.entity_type,
                    'code': row.code,
                    'name_en': row.name_en,
                    'name_zh': row.name_zh,
                    'metadata': row.metadata_json
                }

            # Load relationships (edges)
            result = session.execute(text("""
                SELECT source_id, target_id, relation_type, metadata_json
                FROM entity_relationships
            """))

            for row in result:
                self._graph.add_edge(
                    row.source_id,
                    row.target_id,
                    relation_type=row.relation_type,
                    metadata=row.metadata_json
                )

            self._last_load = current_time
            load_time = time.time() - start_time

            logger.info(
                f"Graph loaded: {self._graph.number_of_nodes()} nodes, "
                f"{self._graph.number_of_edges()} edges, "
                f"load time: {load_time:.2f}s"
            )

            return self._graph

        finally:
            session.close()

    def get_node_info(self, node_id: int) -> Optional[Dict[str, Any]]:
        """
        Get metadata for a specific node.

        Args:
            node_id: Entity ID

        Returns:
            Node metadata dict or None if not found
        """
        graph = self.get_graph()
        if node_id not in graph:
            return None
        return self._node_metadata.get(node_id)

    def find_node_by_code(self, code: str, entity_type: Optional[str] = None) -> Optional[int]:
        """
        Find node ID by entity code.

        Args:
            code: Entity code (e.g., 'AAPL', 'CN')
            entity_type: Optional entity type filter

        Returns:
            Node ID or None if not found
        """
        graph = self.get_graph()

        for node_id, metadata in self._node_metadata.items():
            if metadata['code'] == code:
                if entity_type is None or metadata['entity_type'] == entity_type:
                    return node_id

        return None

    def bfs_traversal(
        self,
        start_node: int,
        max_depth: int = 3,
        entity_type_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Perform BFS traversal from start node.

        Args:
            start_node: Starting node ID
            max_depth: Maximum traversal depth
            entity_type_filter: Optional filter by entity type

        Returns:
            List of entity dicts with depth information
        """
        graph = self.get_graph()

        if start_node not in graph:
            return []

        results = []
        visited = set()
        queue = deque([(start_node, 0)])  # (node_id, depth)

        while queue:
            node_id, depth = queue.popleft()

            if depth > max_depth:
                continue

            if node_id in visited:
                continue

            visited.add(node_id)

            # Get node metadata
            metadata = self._node_metadata.get(node_id, {})

            # Apply entity type filter
            if entity_type_filter and metadata.get('entity_type') != entity_type_filter:
                # Still traverse neighbors, but don't add to results
                for neighbor in graph.neighbors(node_id):
                    if neighbor not in visited:
                        queue.append((neighbor, depth + 1))
                continue

            # Add to results
            results.append({
                'id': node_id,
                'depth': depth,
                'entity_type': metadata.get('entity_type'),
                'code': metadata.get('code'),
                'name_en': metadata.get('name_en'),
                'name_zh': metadata.get('name_zh')
            })

            # Add neighbors to queue
            for neighbor in graph.neighbors(node_id):
                if neighbor not in visited:
                    queue.append((neighbor, depth + 1))

        return results

    def dfs_traversal(
        self,
        start_node: int,
        max_depth: int = 3,
        entity_type_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Perform DFS traversal from start node.

        Args:
            start_node: Starting node ID
            max_depth: Maximum traversal depth
            entity_type_filter: Optional filter by entity type

        Returns:
            List of entity dicts in DFS order
        """
        graph = self.get_graph()

        if start_node not in graph:
            return []

        results = []
        visited = set()

        def dfs(node_id: int, depth: int):
            if depth > max_depth or node_id in visited:
                return

            visited.add(node_id)

            # Get node metadata
            metadata = self._node_metadata.get(node_id, {})

            # Apply entity type filter
            if entity_type_filter is None or metadata.get('entity_type') == entity_type_filter:
                results.append({
                    'id': node_id,
                    'depth': depth,
                    'entity_type': metadata.get('entity_type'),
                    'code': metadata.get('code'),
                    'name_en': metadata.get('name_en'),
                    'name_zh': metadata.get('name_zh')
                })

            # Recurse on neighbors
            for neighbor in graph.neighbors(node_id):
                if neighbor not in visited:
                    dfs(neighbor, depth + 1)

        dfs(start_node, 0)
        return results

    def get_neighbors(
        self,
        node_id: int,
        entity_type_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all neighbors of a node.

        Args:
            node_id: Entity ID
            entity_type_filter: Optional filter by entity type

        Returns:
            List of neighbor entity dicts with relationship info
        """
        graph = self.get_graph()

        if node_id not in graph:
            return []

        results = []

        for neighbor in graph.neighbors(node_id):
            metadata = self._node_metadata.get(neighbor, {})

            # Apply entity type filter
            if entity_type_filter and metadata.get('entity_type') != entity_type_filter:
                continue

            # Get edge metadata
            edge_data = graph[node_id][neighbor]

            results.append({
                'id': neighbor,
                'entity_type': metadata.get('entity_type'),
                'code': metadata.get('code'),
                'name_en': metadata.get('name_en'),
                'name_zh': metadata.get('name_zh'),
                'relation_type': edge_data.get('relation_type'),
                'relation_metadata': edge_data.get('metadata')
            })

        return results

    def shortest_path(
        self,
        start_node: int,
        end_node: int
    ) -> List[Dict[str, Any]]:
        """
        Find shortest path between two nodes.

        Args:
            start_node: Starting node ID
            end_node: Ending node ID

        Returns:
            List of entity dicts representing the path, or empty list if no path
        """
        graph = self.get_graph()

        if start_node not in graph or end_node not in graph:
            return []

        try:
            path = nx.shortest_path(graph, start_node, end_node)

            results = []
            for node_id in path:
                metadata = self._node_metadata.get(node_id, {})
                results.append({
                    'id': node_id,
                    'entity_type': metadata.get('entity_type'),
                    'code': metadata.get('code'),
                    'name_en': metadata.get('name_en'),
                    'name_zh': metadata.get('name_zh')
                })

            return results

        except nx.NetworkXNoPath:
            return []

    def get_subgraph_by_type(
        self,
        entity_type: str,
        max_nodes: int = 100
    ) -> Dict[str, Any]:
        """
        Extract subgraph containing only entities of a specific type.

        Args:
            entity_type: Entity type to filter
            max_nodes: Maximum number of nodes to return

        Returns:
            Dict with nodes and edges
        """
        graph = self.get_graph()

        # Find nodes of the specified type
        target_nodes = [
            node_id for node_id, metadata in self._node_metadata.items()
            if metadata.get('entity_type') == entity_type
        ][:max_nodes]

        if not target_nodes:
            return {'nodes': [], 'edges': []}

        # Extract subgraph
        subgraph = graph.subgraph(target_nodes)

        # Format results
        nodes = []
        for node_id in subgraph.nodes():
            metadata = self._node_metadata.get(node_id, {})
            nodes.append({
                'id': node_id,
                'entity_type': metadata.get('entity_type'),
                'code': metadata.get('code'),
                'name_en': metadata.get('name_en'),
                'name_zh': metadata.get('name_zh')
            })

        edges = []
        for u, v, data in subgraph.edges(data=True):
            edges.append({
                'source': u,
                'target': v,
                'relation_type': data.get('relation_type')
            })

        return {'nodes': nodes, 'edges': edges}

    def get_ego_graph(
        self,
        center_node: int,
        radius: int = 2
    ) -> Dict[str, Any]:
        """
        Extract ego graph centered on a specific node.

        Args:
            center_node: Center node ID
            radius: Radius of the ego graph

        Returns:
            Dict with nodes and edges
        """
        graph = self.get_graph()

        if center_node not in graph:
            return {'nodes': [], 'edges': []}

        # Extract ego graph
        ego_graph = nx.ego_graph(graph, center_node, radius=radius)

        # Format results
        nodes = []
        for node_id in ego_graph.nodes():
            metadata = self._node_metadata.get(node_id, {})
            nodes.append({
                'id': node_id,
                'entity_type': metadata.get('entity_type'),
                'code': metadata.get('code'),
                'name_en': metadata.get('name_en'),
                'name_zh': metadata.get('name_zh'),
                'is_center': node_id == center_node
            })

        edges = []
        for u, v, data in ego_graph.edges(data=True):
            edges.append({
                'source': u,
                'target': v,
                'relation_type': data.get('relation_type')
            })

        return {'nodes': nodes, 'edges': edges}

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get graph statistics.

        Returns:
            Dict with graph statistics
        """
        graph = self.get_graph()

        # Count entities by type
        entity_type_counts = {}
        for metadata in self._node_metadata.values():
            entity_type = metadata.get('entity_type', 'unknown')
            entity_type_counts[entity_type] = entity_type_counts.get(entity_type, 0) + 1

        # Count relationships by type
        relation_type_counts = {}
        for u, v, data in graph.edges(data=True):
            relation_type = data.get('relation_type', 'unknown')
            relation_type_counts[relation_type] = relation_type_counts.get(relation_type, 0) + 1

        # Compute connectivity
        if graph.number_of_nodes() > 0:
            num_components = nx.number_connected_components(graph)
            largest_component = max(nx.connected_components(graph), key=len)
            largest_component_size = len(largest_component)
        else:
            num_components = 0
            largest_component_size = 0

        return {
            'node_count': graph.number_of_nodes(),
            'edge_count': graph.number_of_edges(),
            'average_degree': (2 * graph.number_of_edges() / graph.number_of_nodes()) if graph.number_of_nodes() > 0 else 0,
            'entity_type_counts': entity_type_counts,
            'relation_type_counts': relation_type_counts,
            'connected_components': num_components,
            'largest_component_size': largest_component_size
        }

# Graph Search User Guide

This guide explains how to use the NetworkX-based graph search functionality in fd-open-data-mcp.

## Overview

The graph search system provides powerful graph-based entity relationship queries using NetworkX. It loads entities and relationships from the database into an in-memory graph, enabling fast traversal and pathfinding operations.

## Features

- **BFS Traversal**: Breadth-first search from a starting entity
- **DFS Traversal**: Depth-first search from a starting entity
- **Shortest Path**: Find the shortest path between two entities
- **Neighbor Discovery**: Get all directly connected entities
- **Subgraph Extraction**: Extract subgraphs by entity type
- **Ego Graph**: Extract ego graphs centered on an entity
- **Graph Statistics**: Get comprehensive graph statistics

## MCP Tools

### 1. graph_search

Perform graph-based entity relationship queries.

**Parameters:**
- `algorithm`: Graph algorithm to use
  - `"bfs"`: Breadth-first search traversal
  - `"dfs"`: Depth-first search traversal
  - `"neighbors"`: Get direct neighbors
  - `"shortest_path"`: Find shortest path between two entities
  - `"subgraph"`: Extract subgraph by entity type
  - `"ego_graph"`: Extract ego graph centered on entity
  - `"statistics"`: Get graph statistics
- `start_entity_code`: Starting entity code (e.g., "AAPL", "CN")
- `end_entity_code`: Ending entity code (required for shortest_path)
- `max_depth`: Maximum traversal depth (default: 3)
- `entity_type_filter`: Filter results by entity type (e.g., "country", "stock")

**Examples:**

```python
# BFS traversal from Apple
graph_search("bfs", "AAPL", max_depth=2)

# Shortest path from Apple to China
graph_search("shortest_path", "AAPL", "CN")

# Get all neighbors of Apple
graph_search("neighbors", "AAPL")

# Get graph statistics
graph_search("statistics", "")

# Extract subgraph of all countries
graph_search("subgraph", "", entity_type_filter="country")

# Extract ego graph centered on Apple
graph_search("ego_graph", "AAPL", max_depth=2)
```

**Return Format:**

```json
{
  "algorithm": "bfs",
  "start_entity": "AAPL",
  "max_depth": 2,
  "results": [
    {
      "id": 1,
      "depth": 0,
      "entity_type": "company",
      "code": "AAPL",
      "name_en": "Apple Inc.",
      "name_zh": "苹果公司"
    },
    ...
  ],
  "count": 15
}
```

## Performance

- **Graph Loading**: ~0.2s for 5,333 entities
- **Memory Usage**: ~520MB for current dataset
- **Query Performance**: < 1ms for most operations
- **Cache TTL**: 5 minutes (configurable)

## Configuration

Environment variables:
- `FD_OPEN_DATA_MCP_DATABASE_URL`: Database connection URL
- `GRAPH_CACHE_TTL`: Cache time-to-live in seconds (default: 300)

## Use Cases

### 1. Entity Relationship Exploration

Explore how entities are connected:

```python
# Find all entities related to Apple within 3 hops
result = graph_search("bfs", "AAPL", max_depth=3)
```

### 2. Path Finding

Find the shortest relationship path between entities:

```python
# Find shortest path from Apple to China
path = graph_search("shortest_path", "AAPL", "CN")
```

### 3. Network Analysis

Analyze the entity network structure:

```python
# Get comprehensive graph statistics
stats = graph_search("statistics", "")
# Returns: node_count, edge_count, connected_components, etc.
```

### 4. Subgraph Extraction

Extract specific entity types:

```python
# Extract all technology companies
tech_companies = graph_search("subgraph", "", entity_type_filter="company")
```

## Implementation Details

### EntityGraphManager

The core class that manages the NetworkX graph:

```python
from fd_open_data_mcp.graph.manager import EntityGraphManager

# Initialize with database URL
manager = EntityGraphManager(
    database_url="postgresql://...",
    cache_ttl=300  # 5 minutes
)

# Get the graph (loads from DB if needed)
graph = manager.get_graph()

# Perform operations
result = manager.bfs_traversal(start_node=1, max_depth=3)
```

### Caching

The graph is cached in memory with a configurable TTL:
- Default TTL: 300 seconds (5 minutes)
- Automatic refresh when cache expires
- Force reload: `manager.get_graph(force_reload=True)`

### Graph Structure

- **Nodes**: Entities (companies, countries, stocks, etc.)
- **Edges**: Relationships (belongs_to, located_in, etc.)
- **Node Attributes**: entity_type, code, name_en, name_zh, metadata
- **Edge Attributes**: relation_type, metadata

## Troubleshooting

### Graph Not Loading

If the graph fails to load:
1. Check database connection
2. Verify entities and entity_relationships tables exist
3. Check logs for detailed error messages

### Performance Issues

If queries are slow:
1. Check cache status: `manager._last_load`
2. Force reload if needed: `manager.get_graph(force_reload=True)`
3. Consider increasing cache TTL

### Memory Issues

If memory usage is too high:
1. Reduce cache TTL
2. Consider filtering entities during load
3. Monitor graph size: `graph.number_of_nodes()`, `graph.number_of_edges()`

## Examples

### Example 1: Find All Related Entities

```python
from fd_open_data_mcp.graph.manager import EntityGraphManager

manager = EntityGraphManager("postgresql://...")

# Find all entities related to Apple
result = manager.bfs_traversal(
    start_node=manager.find_node_by_code("AAPL"),
    max_depth=2
)

for entity in result:
    print(f"{entity['code']}: {entity['name_en']} (depth: {entity['depth']})")
```

### Example 2: Shortest Path Analysis

```python
# Find shortest path from Apple to China
path = manager.shortest_path(
    start_node=manager.find_node_by_code("AAPL"),
    end_node=manager.find_node_by_code("CN")
)

print("Path:")
for i, entity in enumerate(path):
    print(f"{i+1}. {entity['code']}: {entity['name_en']}")
```

### Example 3: Graph Statistics

```python
stats = manager.get_statistics()

print(f"Nodes: {stats['node_count']}")
print(f"Edges: {stats['edge_count']}")
print(f"Connected Components: {stats['connected_components']}")
print(f"Largest Component: {stats['largest_component_size']}")
```

## Next Steps

- Explore semantic search for entity discovery
- Combine graph search with semantic search for hybrid queries
- Use graph statistics for network analysis
- Implement custom graph algorithms for specific use cases

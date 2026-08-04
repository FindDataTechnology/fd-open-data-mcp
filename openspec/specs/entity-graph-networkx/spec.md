# entity-graph-networkx Specification

## Purpose
TBD - created by archiving change networkx-graph-search. Update Purpose after archive.
## Requirements
### Requirement: Graph manager with NetworkX
The system SHALL provide a graph manager that uses NetworkX to load entities and relationships from the database and perform graph operations.

#### Scenario: Graph initialization
- **WHEN** the system starts
- **THEN** it SHALL load all entities and relationships from the database into a NetworkX graph
- **AND** the graph SHALL be cached in memory for subsequent queries

#### Scenario: Graph refresh
- **WHEN** the graph cache is older than 5 minutes
- **THEN** the system SHALL reload the graph from the database
- **AND** the old graph SHALL be discarded

### Requirement: Graph traversal operations
The system SHALL support graph traversal operations including BFS, DFS, and neighbor discovery.

#### Scenario: BFS traversal
- **WHEN** a user requests BFS traversal from a starting entity
- **THEN** the system SHALL return all entities within the specified depth
- **AND** the result SHALL include entity metadata (type, code, name)

#### Scenario: DFS traversal
- **WHEN** a user requests DFS traversal from a starting entity
- **THEN** the system SHALL return entities in DFS order
- **AND** the result SHALL include the traversal path

#### Scenario: Neighbor discovery
- **WHEN** a user requests neighbors of an entity
- **THEN** the system SHALL return all directly connected entities
- **AND** the result SHALL include relationship types

### Requirement: Shortest path algorithm
The system SHALL support shortest path calculation between two entities.

#### Scenario: Shortest path query
- **WHEN** a user requests shortest path between entity A and entity B
- **THEN** the system SHALL return the shortest path as a list of entities
- **AND** the result SHALL include the path length and relationship types

#### Scenario: No path exists
- **WHEN** no path exists between entity A and entity B
- **THEN** the system SHALL return an empty path
- **AND** the system SHALL indicate that no path was found

### Requirement: Subgraph extraction
The system SHALL support extracting subgraphs based on entity type or depth.

#### Scenario: Subgraph by entity type
- **WHEN** a user requests a subgraph of a specific entity type
- **THEN** the system SHALL return all entities of that type and their relationships
- **AND** the result SHALL be a NetworkX graph object

#### Scenario: Ego graph extraction
- **WHEN** a user requests an ego graph centered on an entity
- **THEN** the system SHALL return the entity and all entities within the specified radius
- **AND** the result SHALL include all relationships within the subgraph

### Requirement: Graph statistics
The system SHALL provide graph statistics including node count, edge count, and connectivity metrics.

#### Scenario: Basic statistics
- **WHEN** a user requests graph statistics
- **THEN** the system SHALL return node count, edge count, and average degree
- **AND** the statistics SHALL be computed from the current graph

#### Scenario: Connectivity analysis
- **WHEN** a user requests connectivity analysis
- **THEN** the system SHALL return the number of connected components
- **AND** the system SHALL return the size of the largest connected component

### Requirement: Graph query MCP tool
The system SHALL provide an MCP tool for graph queries that supports multiple algorithms.

#### Scenario: Graph query tool
- **WHEN** a user calls the graph_search MCP tool
- **THEN** the system SHALL execute the specified algorithm (bfs, dfs, shortest_path, neighbors)
- **AND** the system SHALL return the results in a structured format

#### Scenario: Algorithm selection
- **WHEN** a user specifies an algorithm parameter
- **THEN** the system SHALL use the specified algorithm
- **AND** the system SHALL validate that the algorithm is supported


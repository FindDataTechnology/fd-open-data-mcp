## ADDED Requirements

### Requirement: Entity embedding generation
The system SHALL generate vector embeddings for all entities using a sentence transformer model.

#### Scenario: Batch embedding generation
- **WHEN** the embedding generation process is triggered
- **THEN** the system SHALL generate 384-dimensional embeddings for all entities
- **AND** the embeddings SHALL be stored in the entity_embeddings table

#### Scenario: Incremental embedding update
- **WHEN** a new entity is added to the database
- **THEN** the system SHALL generate an embedding for the new entity
- **AND** the embedding SHALL be inserted into the entity_embeddings table

### Requirement: Entity semantic search
The system SHALL support semantic search for entities using vector similarity.

#### Scenario: Semantic search query
- **WHEN** a user performs a semantic search for entities
- **THEN** the system SHALL encode the query using the same model
- **AND** the system SHALL compute cosine similarity with all entity embeddings
- **AND** the system SHALL return the top-K most similar entities

#### Scenario: Filter by entity type
- **WHEN** a user specifies an entity_type filter
- **THEN** the system SHALL only return entities of that type
- **AND** the results SHALL be ranked by similarity score

### Requirement: Unified semantic search
The system SHALL provide a unified semantic search that searches both entities and concepts.

#### Scenario: Unified search query
- **WHEN** a user performs a semantic search without specifying entity_type
- **THEN** the system SHALL search both entities and concepts
- **AND** the results SHALL include both entities and concepts
- **AND** the results SHALL be ranked by similarity score

#### Scenario: Result formatting
- **WHEN** the unified search returns results
- **THEN** each result SHALL include type (entity/concept), code, name, and similarity score
- **AND** the results SHALL be sorted by similarity in descending order

### Requirement: Embedding model configuration
The system SHALL support configurable embedding models.

#### Scenario: Model selection
- **WHEN** the system initializes the embedding generator
- **THEN** the system SHALL load the configured model (default: all-MiniLM-L6-v2)
- **AND** the system SHALL validate that the model is available

#### Scenario: Model fallback
- **WHEN** the primary model is not available
- **THEN** the system SHALL fall back to a backup model
- **AND** the system SHALL log a warning about the fallback

### Requirement: Semantic search MCP tool
The system SHALL provide an MCP tool for semantic search of entities and concepts.

#### Scenario: Semantic search tool
- **WHEN** a user calls the semantic_search_entities MCP tool
- **THEN** the system SHALL perform semantic search on entities
- **AND** the system SHALL return results with similarity scores

#### Scenario: Unified search tool
- **WHEN** a user calls the semantic_search_unified MCP tool
- **THEN** the system SHALL search both entities and concepts
- **AND** the system SHALL return combined results

### Requirement: Embedding cache
The system SHALL cache embeddings in memory to improve query performance.

#### Scenario: Cache initialization
- **WHEN** the semantic search system starts
- **THEN** the system SHALL load all embeddings into memory
- **AND** the cache SHALL be used for subsequent queries

#### Scenario: Cache invalidation
- **WHEN** embeddings are updated in the database
- **THEN** the system SHALL invalidate the cache
- **AND** the system SHALL reload embeddings on the next query

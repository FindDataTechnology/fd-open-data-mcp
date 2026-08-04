## 1. Entity Graph with NetworkX

### 1.1 Graph Manager Implementation
- [x] 1.1.1 Create `fd_open_data_mcp/graph/manager.py` with EntityGraphManager class
- [x] 1.1.2 Implement graph loading from database (entities + relationships)
- [x] 1.1.3 Implement graph caching mechanism (5-minute TTL)
- [x] 1.1.4 Add graph refresh logic

### 1.2 Graph Traversal Operations
- [x] 1.2.1 Implement BFS traversal method
- [x] 1.2.2 Implement DFS traversal method
- [x] 1.2.3 Implement neighbor discovery method
- [x] 1.2.4 Add support for depth-limited traversal

### 1.3 Graph Algorithms
- [x] 1.3.1 Implement shortest path algorithm (Dijkstra)
- [x] 1.3.2 Implement subgraph extraction by entity type
- [x] 1.3.3 Implement ego graph extraction (radius-based)
- [x] 1.3.4 Implement graph statistics (node count, edge count, connectivity)

### 1.4 Graph Query MCP Tool
- [x] 1.4.1 Create `graph_search` MCP tool
- [x] 1.4.2 Add algorithm parameter validation
- [x] 1.4.3 Implement result formatting
- [x] 1.4.4 Add error handling for invalid queries

## 2. Entity Semantic Search

### 2.1 Embedding Generation
- [x] 2.1.1 Create `fd_open_data_mcp/embeddings/generator.py` with EntityEmbeddingGenerator class
- [x] 2.1.2 Implement batch embedding generation for all entities
- [x] 2.1.3 Create entity_embeddings table schema
- [x] 2.1.4 Implement incremental embedding update for new entities

### 2.2 Semantic Search Implementation
- [x] 2.2.1 Create `fd_open_data_mcp/semantic/entity_search.py` with EntitySemanticSearch class
- [x] 2.2.2 Implement cosine similarity computation
- [x] 2.2.3 Implement top-K retrieval
- [x] 2.2.4 Add entity_type filter support

### 2.3 Unified Semantic Search
- [x] 2.3.1 Create unified search that combines entity and concept search
- [x] 2.3.2 Implement result merging and ranking
- [x] 2.3.3 Add result formatting (type, code, name, similarity)

### 2.4 Semantic Search MCP Tools
- [x] 2.4.1 Create `semantic_search_entities` MCP tool
- [x] 2.4.2 Create `semantic_search_unified` MCP tool
- [x] 2.4.3 Add query parameter validation
- [x] 2.4.4 Implement result formatting

### 2.5 Embedding Cache
- [x] 2.5.1 Implement in-memory embedding cache
- [x] 2.5.2 Add cache invalidation logic
- [x] 2.5.3 Implement cache warm-up on startup

## 3. Database Schema

### 3.1 Entity Embeddings Table
- [x] 3.1.1 Create migration script for entity_embeddings table
- [x] 3.1.2 Add indexes for fast similarity search
- [x] 3.1.3 Test migration on both PostgreSQL and SQLite

## 4. Integration and Testing

### 4.1 Integration
- [x] 4.1.1 Integrate graph manager with sync engine
- [x] 4.1.2 Integrate embedding generator with sync engine
- [x] 4.1.3 Update MCP server to register new tools
- [x] 4.1.4 Add configuration options (model selection, cache TTL)

### 4.2 Unit Tests
- [x] 4.2.1 Create tests for EntityGraphManager
- [x] 4.2.2 Create tests for graph traversal operations
- [x] 4.2.3 Create tests for shortest path algorithm
- [x] 4.2.4 Create tests for EntityEmbeddingGenerator
- [x] 4.2.5 Create tests for EntitySemanticSearch

### 4.3 Integration Tests
- [x] 4.3.1 Test graph search end-to-end
- [x] 4.3.2 Test semantic search end-to-end
- [x] 4.3.3 Test unified search end-to-end
- [x] 4.3.4 Test with both PostgreSQL and SQLite

## 5. Documentation

### 5.1 User Guide
- [x] 5.1.1 Create graph search user guide
- [x] 5.1.2 Create semantic search user guide
- [x] 5.1.3 Add examples and use cases
- [x] 5.1.4 Document configuration options

### 5.2 API Documentation
- [x] 5.2.1 Document EntityGraphManager API
- [x] 5.2.2 Document EntitySemanticSearch API
- [x] 5.2.3 Document MCP tools
- [x] 5.2.4 Add code examples

## 6. Performance Optimization

### 6.1 Graph Performance
- [x] 6.1.1 Profile graph loading performance
- [x] 6.1.2 Optimize graph caching strategy
- [x] 6.1.3 Add graph query benchmarks

### 6.2 Semantic Search Performance
- [x] 6.2.1 Profile embedding generation performance
- [x] 6.2.2 Optimize similarity computation
- [x] 6.2.3 Add semantic search benchmarks

## 7. Deployment

### 7.1 Configuration
- [x] 7.1.1 Add environment variables for configuration
- [x] 7.1.2 Update .env.example with new options
- [x] 7.1.3 Add configuration validation

### 7.2 Deployment Scripts
- [x] 7.2.1 Update migration scripts to include entity_embeddings
- [x] 7.2.2 Add embedding generation to deployment process
- [x] 7.2.3 Update Docker configuration if needed

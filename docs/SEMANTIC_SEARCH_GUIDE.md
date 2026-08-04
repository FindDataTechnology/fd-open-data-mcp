# Semantic Search User Guide

This guide explains how to use the semantic search functionalities in fd-open-data-mcp for both entities and concepts.

## Overview

The semantic search system uses vector embeddings to enable natural language search across entities (companies, countries, stocks, etc.) and concepts (indicators like GDP, CPI, etc.). It leverages the `all-MiniLM-L6-v2` sentence transformer model to generate 384-dimensional embeddings and performs cosine similarity matching.

## Features

- **Entity Semantic Search**: Search entities using natural language
- **Concept Semantic Search**: Search concepts using natural language
- **Unified Search**: Search both entities and concepts simultaneously
- **Entity Type Filtering**: Filter results by entity type
- **Caching**: In-memory embedding cache for fast repeated queries
- **Top-K Retrieval**: Return top-K most similar results

## MCP Tools

### 1. semantic_search_entities

Search entities using semantic similarity.

**Parameters:**
- `query`: Natural language query (e.g., "Asian countries", "technology companies")
- `entity_type`: Optional filter by entity type (country, stock, industry, etc.)
- `limit`: Maximum number of results to return (default: 20)

**Examples:**

```python
# Search for Asian countries
semantic_search_entities("Asian countries", entity_type="country")

# Search for technology companies
semantic_search_entities("technology companies", entity_type="company")

# Search for inflation indicators (returns entities)
semantic_search_entities("inflation indicators")
```

**Return Format:**

```json
[
  {
    "id": 1,
    "entity_type": "country",
    "code": "CN",
    "name_en": "China",
    "name_zh": "中国",
    "metadata": {"region": "Asia"},
    "similarity": 0.85
  },
  ...
]
```

### 2. semantic_search_unified

Unified semantic search across both entities and concepts.

**Parameters:**
- `query`: Natural language query
- `entity_type`: Optional filter by entity type (applies to entities only)
- `limit`: Maximum number of results to return (default: 20)

**Examples:**

```python
# Search for inflation-related entities and concepts
semantic_search_unified("inflation indicators")
# Returns: [CPI_YOY (concept), Asian countries (entities), ...]

# Search for technology companies and related concepts
semantic_search_unified("technology companies")
```

**Return Format:**

```json
[
  {
    "result_type": "concept",
    "id": 1,
    "code": "CPI_YOY",
    "name_en": "CPI YoY",
    "name_zh": "居民消费价格指数(同比)",
    "similarity": 0.85
  },
  {
    "result_type": "entity",
    "id": 1,
    "entity_type": "country",
    "code": "CN",
    "name_en": "China",
    "name_zh": "中国",
    "similarity": 0.82
  },
  ...
]
```

## Performance

- **Embedding Generation**: ~1ms per query (with cache)
- **Similarity Computation**: ~10ms for 5,333 entities
- **Cache Hit Rate**: > 90% for repeated queries
- **Memory Usage**: ~50MB for embedding cache

## Configuration

Environment variables:
- `FD_OPEN_DATA_MCP_DATABASE_URL`: Database connection URL
- `EMBEDDING_MODEL`: Model name (default: "all-MiniLM-L6-v2")

## Implementation Details

### EntitySemanticSearch

The core class that manages semantic search:

```python
from fd_open_data_mcp.semantic.entity_search import EntitySemanticSearch

# Initialize with database URL
search = EntitySemanticSearch(
    database_url="postgresql://...",
    model_name="all-MiniLM-L6-v2"
)

# Search entities
results = search.search("Asian countries", entity_type="country", limit=10)

# Unified search
results = search.search_unified("inflation indicators", limit=10)
```

### EntityEmbeddingGenerator

Generates embeddings for entities:

```python
from fd_open_data_mcp.embeddings.generator import EntityEmbeddingGenerator

# Initialize generator
generator = EntityEmbeddingGenerator(model_name="all-MiniLM-L6-v2")

# Generate embeddings for all entities
result = generator.generate_all_entity_embeddings(
    database_url="postgresql://...",
    batch_size=100
)
```

### Caching

The system uses in-memory caching for embeddings:
- Cache size: Up to 1,000 embeddings
- Cache invalidation: Manual via `search.invalidate_cache()`
- Cache statistics: `search.get_cache_stats()`

### Similarity Computation

Cosine similarity is used for matching:

```python
similarity = dot(A, B) / (||A|| * ||B||)
```

Where A and B are embedding vectors.

## Use Cases

### 1. Entity Discovery

Discover entities using natural language:

```python
# Find countries in Asia
results = semantic_search_entities("Asian countries", entity_type="country")
```

### 2. Concept Discovery

Discover concepts using natural language:

```python
# Find inflation-related concepts
results = semantic_search("inflation indicators")
```

### 3. Hybrid Search

Search both entities and concepts:

```python
# Find all inflation-related data
results = semantic_search_unified("inflation indicators")
```

### 4. Filtered Search

Filter by entity type:

```python
# Find only technology companies
results = semantic_search_entities("technology", entity_type="company")
```

## Troubleshooting

### No Results Found

If no results are found:
1. Check if embeddings exist in entity_embeddings table
2. Verify the query is meaningful
3. Try a different query or broader terms

### Slow Performance

If search is slow:
1. Check cache hit rate: `search.get_cache_stats()`
2. Verify database connection
3. Consider increasing cache size

### Low Similarity Scores

If similarity scores are low:
1. Try more specific queries
2. Check if embeddings are up to date
3. Regenerate embeddings if needed

## Examples

### Example 1: Find Related Entities

```python
from fd_open_data_mcp.semantic.entity_search import EntitySemanticSearch

search = EntitySemanticSearch("postgresql://...")

# Find countries similar to China
results = search.search("China economy", entity_type="country", limit=5)

for result in results:
    print(f"{result['code']}: {result['name_en']} (similarity: {result['similarity']:.2f})")
```

### Example 2: Unified Search

```python
# Search for inflation-related data
results = search.search_unified("inflation indicators", limit=10)

for result in results:
    if result['result_type'] == 'concept':
        print(f"Concept: {result['code']} - {result['name_en']}")
    else:
        print(f"Entity: {result['code']} - {result['name_en']}")
```

### Example 3: Cache Management

```python
# Get cache statistics
stats = search.get_cache_stats()
print(f"Cache size: {stats['cache_size']}")
print(f"Hit rate: {stats['hit_rate']:.2%}")

# Invalidate cache
search.invalidate_cache()
```

## Database Schema

### entity_embeddings Table

```sql
CREATE TABLE entity_embeddings (
    id SERIAL PRIMARY KEY,
    entity_id INTEGER REFERENCES entities(id),
    embedding TEXT,  -- JSON array of floats
    model VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(entity_id, model)
);
```

### concept_embeddings Table

```sql
CREATE TABLE concept_embeddings (
    id SERIAL PRIMARY KEY,
    concept_id INTEGER REFERENCES concepts(id),
    embedding TEXT,  -- JSON array of floats
    model VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(concept_id, model)
);
```

## Next Steps

- Combine semantic search with graph search for hybrid queries
- Use semantic search for entity discovery before graph traversal
- Implement custom embedding models for domain-specific search
- Explore vector database integration for larger datasets

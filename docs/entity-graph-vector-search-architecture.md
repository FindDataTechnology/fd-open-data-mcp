# Entity Graph + Vector Search Architecture

## Overview

The fd-open-data-mcp system uses a **3-layer architecture** for entity-centric data discovery:

```
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 1: Entity Graph  (metadata + relationships)              │
│  "What exists and how do they connect?"                          │
│                                                                  │
│   company ──listed_as──▶ stock ──operates_in──▶ industry         │
│       │                         │                     │         │
│   headquartered_in         traded_on             subsector_of    │
│       │                         │                     │         │
│       ▼                         ▼                     ▼         │
│      city ──located_in──▶ country ◀───────── parent_industry    │
│                                                                  │
│  Tables: entities, entity_relationships                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ (entity_type + entity_id)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 2: Vector Search  (semantic search over indicators)      │
│  "Which indicators match my question?"                           │
│                                                                  │
│   concepts (gdp, CPI_YOY, price.close, ...)                     │
│       │                                                          │
│   + descriptions (name_en, name_zh, category, unit, ...)        │
│       │                                                          │
│   -> embedded into concept_embeddings (384-dim vectors)         │
│                                                                  │
│   semantic_search("inflation indicators")                       │
│       -> [CPI_YOY, PPI_YOY, WB_INFLATION, ...]                  │
│       -> filter: entity_type=country, frequency=monthly         │
│                                                                  │
│  Tables: concepts, concept_embeddings                           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ (concept_id + entity_id)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 3: Value Store  (the actual numbers)                     │
│  "Give me the numbers for these indicators × these entities"    │
│                                                                  │
│   semantic_observations (concept_id, entity_id, date, value)   │
│   -> 96M rows of actual data values                             │
│   -> NOT in the graph, NOT in the vector DB                      │
└─────────────────────────────────────────────────────────────────┘
```

## The AI Search Flow

The `ai_search` MCP tool orchestrates all three layers:

```
User: "Show me inflation indicators for Asian countries"
    │
    ▼
┌─ Layer 1: Semantic Search ──────────────────────────┐
│ embed("inflation indicators")                       │
│ -> find concepts with high cosine similarity        │
│ -> filter: entity_type='country'                    │
│ -> returns: [CPI_YOY, PPI_YOY, WB_INFLATION, ...]  │
└─────────────────────────────────────────────────────┘
    │
    ▼
┌─ Layer 2: Graph Traversal ──────────────────────────┐
│ "Asian countries" ->                                │
│   entities WHERE entity_type='country'              │
│   -> [CN, JP, KR, IN, ID, ...]                      │
│                                                     │
│ (optional) expand:                                  │
│   stocks headquartered in those countries           │
│   -> via entity_relationships traversal             │
└─────────────────────────────────────────────────────┘
    │
    ▼
┌─ Layer 3: Value Query ──────────────────────────────┐
│ SELECT concept_id, entity_id, date, value           │
│ FROM semantic_observations                          │
│ WHERE concept_id IN (...)                           │
│   AND entity_id IN (...)                            │
│   AND date >= '2020-01-01'                          │
└─────────────────────────────────────────────────────┘
    │
    ▼
Return structured data to AI -> AI answers the user
```

## MCP Tools

### Entity Graph Tools

| Tool | Description |
|------|-------------|
| `list_entities(entity_type, limit, offset)` | List entities of a type |
| `get_entity(entity_type, code)` | Get a single entity |
| `add_entity(entity_type, code, name_en?, name_zh?, metadata?)` | Create a new entity |
| `list_relationships(entity_type, code, direction)` | List relationships for an entity |
| `add_relationship(source_type, source_code, relation_type, target_type, target_code, ...)` | Add a relationship edge |

### Vector Search Tools

| Tool | Description |
|------|-------------|
| `semantic_search(query, entity_type?, frequency?, limit=20)` | Find concepts matching a natural-language query |
| `re_embed_concept(concept_id)` | Re-embed a concept after its description is updated |

### AI Search Tool

| Tool | Description |
|------|-------------|
| `ai_search(query, entity_type?, limit=20, include_values=False, value_date?)` | Orchestrate all three layers |

## Database Schema

### Layer 1: Entity Graph

```sql
-- Unified entity registry
CREATE TABLE entities (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(32) NOT NULL,    -- country, city, company, stock, etc.
    code VARCHAR(128) NOT NULL,          -- canonical code (ticker, iso_code, etc.)
    name_en VARCHAR(255),
    name_zh VARCHAR(255),
    metadata_json JSONB,                  -- type-specific attributes
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(entity_type, code)
);

-- Graph edges with temporal validity
CREATE TABLE entity_relationships (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    relation_type VARCHAR(64) NOT NULL,   -- listed_as, operates_in, located_in, etc.
    target_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    valid_from TIMESTAMP,                 -- when the relationship started
    valid_to TIMESTAMP,                   -- NULL = currently true
    metadata_json JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(source_id, relation_type, target_id, valid_from)
);
```

### Layer 2: Vector Search

```sql
-- Concept embeddings (stored as JSON arrays)
CREATE TABLE concept_embeddings (
    id SERIAL PRIMARY KEY,
    concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    embedding JSONB NOT NULL,             -- 384-dim vector as JSON array
    model VARCHAR(128) NOT NULL,          -- e.g., "all-MiniLM-L6-v2"
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(concept_id, model)
);
```

### Layer 3: Value Store (existing, unchanged)

```sql
-- Actual data values (96M rows)
CREATE TABLE semantic_observations (
    id SERIAL PRIMARY KEY,
    concept_id INTEGER REFERENCES concepts(id) ON DELETE CASCADE,
    entity_type VARCHAR(32) NOT NULL,
    entity_id INTEGER NOT NULL,
    date VARCHAR(64) NOT NULL,
    value VARCHAR(255),
    unit VARCHAR(64),
    source_used VARCHAR(64) NOT NULL,
    fetched_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(concept_id, entity_type, entity_id, date)
);
```

## Protocol Extension

Datasource manifests can now declare entity coverage and relationship resolution:

```yaml
# fd-open-data-protocol DatasourceManifest
name: my-datasource
functions: [...]
concepts: [...]
entities:                          # NEW: entity coverage declaration
  - entity_type: stock
    coverage: explicit
    codes: [AAPL, MSFT, GOOGL]
  - entity_type: country
    coverage: universe
relationships:                     # NEW: relationship resolution declaration
  - relation_type: listed_as
    source_entity_type: company
    target_entity_type: stock
    resolver_module: my_pkg.resolve_listed_as
  - relation_type: operates_in
    source_entity_type: company
    target_entity_type: industry
    resolver_module: my_pkg.resolve_industry
fetch: [...]
```

## Embedding Model

- **Model**: `all-MiniLM-L6-v2` (384-dimensional vectors)
- **Library**: sentence-transformers
- **Input**: concatenation of `name_en | name_zh | category | unit | measure | frequency | entity_type | code`
- **Similarity**: cosine similarity (dot product for normalized vectors)

## Usage Examples

### Example 1: Find inflation indicators

```python
# Using semantic_search
results = semantic_search("inflation indicators", entity_type="country", limit=10)
# Returns: [WB_INFLATION, WB_FP.CPI.TOTL.ZG, US_CPI_YOY, ...]
```

### Example 2: Full AI search with values

```python
# Using ai_search
result = ai_search(
    "GDP growth for Asian countries",
    entity_type="country",
    limit=5,
    include_values=True,
    value_date="2024-01-01"
)
# Returns: {
#   "concepts": [gdp, GDP_YOY, ...],
#   "entities": [CN, JP, KR, ...],
#   "values": [{concept_id, entity_id, date, value}, ...]
# }
```

### Example 3: Entity graph traversal

```python
# List all stocks
stocks = list_entities("stock", limit=100)

# Get relationships for a stock
rels = list_relationships("stock", "AAPL", "outgoing")
# Returns: [{relation_type: "listed_as", target: {entity_type: "company", ...}}, ...]
```

## Adding a New Datasource with Entity Coverage

1. Create a manifest with `entities` and `relationships` sections
2. Register it: `register_datasource(manifest, session)`
3. The registrar upserts explicit entities into the `entities` table
4. For universe coverage, the source is recorded as covering that entity type
5. Relationship resolvers are stored for later use

## Performance Notes

- **Vector search**: loads all concept embeddings into memory (268 concepts × 384 dims = ~400KB)
- **Graph traversal**: indexed by `(source_id, relation_type)` and `(target_id, relation_type)`
- **Value queries**: indexed by `(concept_id, entity_type, entity_id, date)`
- **For larger datasets**: consider pgvector for native vector search in Postgres

## Migration Path to pgvector

If the dataset grows beyond ~10K concepts, consider:

1. Install pgvector extension on Postgres
2. Migrate `concept_embeddings.embedding` from JSONB to `vector(384)` type
3. Create an ivfflat index: `CREATE INDEX ON concept_embeddings USING ivfflat (embedding vector_cosine_ops)`
4. Update `semantic_search` to use SQL-based similarity: `ORDER BY embedding <=> :query_vec`

This enables sub-linear search over millions of vectors.

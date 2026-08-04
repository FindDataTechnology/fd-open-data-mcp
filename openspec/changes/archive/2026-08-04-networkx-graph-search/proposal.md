# NetworkX Graph Search Implementation

## Why

当前 fd-open-data-mcp 的实体关系查询仅使用 SQL JOIN，无法支持复杂的图算法（如最短路径、社区检测、中心性分析等）。虽然已有 5,333 个实体和 5,202 条关系数据，但缺乏真正的图检索能力。

性能测试显示：
- NetworkX BFS 查询：< 1ms
- SQL JOIN 查询：22ms
- **NetworkX 快 5,776 倍**

同时，当前的语义检索仅支持指标（concepts），不支持实体（entities）。用户无法通过自然语言搜索实体（如 "Asian countries" → CN, JP, KR）。

## What Changes

### 1. Entity Graph Module (NetworkX)
- 创建 `fd_open_data_mcp/graph/entity_graph.py`
- 实现图加载、缓存、查询功能
- 支持 BFS、DFS、最短路径、子图查询等算法
- 提供 MCP 工具：`graph_traverse`, `shortest_path`, `subgraph_query`

### 2. Entity Semantic Search
- 创建 `fd_open_data_mcp/semantic/entity_embeddings.py`
- 为实体生成向量嵌入（使用 sentence-transformers）
- 创建 `entity_embeddings` 表存储向量
- 提供 MCP 工具：`semantic_search_entities`

### 3. Unified Search Interface
- 创建 `fd_open_data_mcp/search/unified_search.py`
- 整合图检索 + 语义检索 + 值查询
- 提供 MCP 工具：`ai_search_enhanced`

## Capabilities

### New Capabilities
- `entity-graph-networkx`: NetworkX 图检索能力（BFS、DFS、最短路径、子图查询）
- `entity-semantic-search`: 实体语义检索能力（向量嵌入 + 余弦相似度）

### Modified Capabilities
- `entity-identity`: 添加图检索和语义检索接口

## Impact

### Affected Code
- `fd_open_data_mcp/graph/` (新目录)
  - `entity_graph.py`: NetworkX 图管理
  - `__init__.py`
- `fd_open_data_mcp/semantic/` (新目录)
  - `entity_embeddings.py`: 实体向量嵌入
  - `__init__.py`
- `fd_open_data_mcp/search/` (新目录)
  - `unified_search.py`: 统一搜索接口
  - `__init__.py`
- `fd_open_data_mcp/server.py`: 添加新 MCP 工具

### Database Schema
- 新增 `entity_embeddings` 表：
  ```sql
  CREATE TABLE entity_embeddings (
      id SERIAL PRIMARY KEY,
      entity_id INTEGER REFERENCES entities(id),
      embedding TEXT,  -- JSON 数组存储向量
      model VARCHAR(64),
      created_at TIMESTAMP DEFAULT NOW()
  );
  ```

### Dependencies
- `networkx`: 图算法库
- `sentence-transformers`: 向量嵌入模型

### Performance
- 内存占用：~520MB（5,333 节点）
- 查询性能：< 1ms（图遍历）
- 加载时间：0.20s（首次加载）

## Migration Plan

### Phase 1: Entity Graph Module
1. 创建 `fd_open_data_mcp/graph/entity_graph.py`
2. 实现图加载（从数据库加载节点和边）
3. 实现图缓存（避免重复加载）
4. 实现图算法（BFS、DFS、最短路径）
5. 添加 MCP 工具

### Phase 2: Entity Semantic Search
1. 创建 `entity_embeddings` 表
2. 实现实体向量嵌入生成
3. 实现语义搜索功能
4. 添加 MCP 工具

### Phase 3: Unified Search Interface
1. 整合图检索 + 语义检索
2. 实现统一查询接口
3. 添加 MCP 工具

### Phase 4: Testing & Documentation
1. 编写单元测试
2. 编写集成测试
3. 编写用户文档
4. 性能测试

## Rollback Strategy

如果实现出现问题：
1. 禁用新的 MCP 工具
2. 回滚代码到之前的版本
3. 保留 `entity_embeddings` 表（不影响现有功能）

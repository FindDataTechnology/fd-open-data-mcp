## Context

当前 fd-open-data-mcp 系统已经具备：
- 实体存储（entities 表，5,333 个实体）
- 关系存储（entity_relationships 表，5,202 条关系）
- 指标存储（concepts 表，289 个指标）
- 指标的向量嵌入（concept_embeddings 表，289 个向量）
- 语义检索能力（semantic_search MCP 工具，仅支持指标）

**缺失的能力：**
1. **实体的语义检索** - 无法通过自然语言搜索实体（如 "Asian countries"）
2. **真正的图检索** - 只有 SQL JOIN，没有图遍历、最短路径等算法
3. **统一的查询接口** - 没有同时支持图检索和语义检索的 MCP 工具

**性能测试数据：**
- NetworkX BFS: < 1ms（内存中图遍历）
- SQL JOIN: 22ms（数据库查询）
- NetworkX 比 SQL JOIN 快 5,776 倍（当前数据量）

## Goals / Non-Goals

**Goals:**
- 实现实体的向量嵌入，支持语义搜索实体
- 使用 NetworkX 实现图遍历、最短路径等算法
- 提供统一的 MCP 工具接口（graph_search + semantic_search）
- 保持与现有系统的兼容性（不改变数据库结构）
- 性能目标：图遍历 < 10ms，语义搜索 < 100ms

**Non-Goals:**
- 不引入新的数据库（如 Neo4j、Apache AGE）
- 不改变现有的数据库结构
- 不实现社区检测、中心性分析等高级图算法（可后续扩展）
- 不实现图可视化（可后续扩展）

## Decisions

### D1. 使用 NetworkX 作为图算法库

**Decision:** 使用 NetworkX 而不是 Apache AGE 或 Neo4j

**Rationale:**
- **性能优秀** - 当前数据量下，NetworkX 比 SQL JOIN 快 5,776 倍
- **实现简单** - 不需要安装额外插件，不需要 DBA 权限
- **数据一致性** - 数据仍然存储在 PostgreSQL，NetworkX 只是查询层
- **学习成本低** - NetworkX 文档好，Python 生态成熟
- **适合当前规模** - 5,333 个节点，NetworkX 完全可以处理

**Alternatives considered:**
- **Apache AGE** - 需要安装插件，需要 DBA 权限，当前环境不支持
- **Neo4j** - 需要额外部署和维护，对于 5,333 节点过度设计
- **SQL JOIN** - 性能差（22ms vs < 1ms），不支持复杂图算法

### D2. 实体的向量嵌入

**Decision:** 为实体生成向量嵌入，存储在 entity_embeddings 表

**Rationale:**
- **统一语义检索** - 实体和指标都支持语义搜索
- **复用现有模型** - 使用 all-MiniLM-L6-v2（384 维），与指标嵌入一致
- **性能可接受** - 5,333 个实体，嵌入时间 < 1 分钟

**Implementation:**
```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')

for entity in entities:
    text = f"{entity.name_en} {entity.name_zh} {entity.entity_type}"
    embedding = model.encode(text)  # 384 维向量
    # 存储到 entity_embeddings 表
```

### D3. 图缓存策略

**Decision:** 使用内存缓存，每 5 分钟重新加载一次

**Rationale:**
- **查询速度快** - 图已经在内存中，查询 < 1ms
- **内存占用可接受** - 5,333 节点，~520MB
- **数据新鲜度** - 5 分钟的延迟可以接受（实体变化不频繁）

**Implementation:**
```python
class EntityGraph:
    def __init__(self):
        self._graph = None
        self._last_load = None
    
    def get_graph(self):
        # 每 5 分钟重新加载一次
        if self._graph is None or time.time() - self._last_load > 300:
            self._graph = self._load_from_database()
            self._last_load = time.time()
        return self._graph
```

### D4. 统一的查询接口

**Decision:** 提供两个 MCP 工具：graph_search 和 semantic_search

**Rationale:**
- **职责分离** - graph_search 负责图检索，semantic_search 负责语义检索
- **灵活性高** - 可以单独使用，也可以组合使用
- **向后兼容** - 现有的 semantic_search 工具保持不变

**Implementation:**
```python
@mcp.tool
def graph_search(
    query: str,
    entity_type: str = None,
    max_depth: int = 3,
    algorithm: str = "bfs"  # bfs, dfs, shortest_path
) -> list[dict]:
    """图检索：支持遍历、最短路径等算法"""
    pass

@mcp.tool
def semantic_search_entities(
    query: str,
    entity_type: str = None,
    limit: int = 20
) -> list[dict]:
    """语义检索：搜索实体和指标"""
    pass
```

## Risks / Trade-offs

### Risk 1: 内存占用高

**Risk:** NetworkX 需要 520MB 内存来存储图

**Mitigation:**
- 当前服务器内存充足（> 8GB）
- 可以配置缓存过期时间，避免长时间占用
- 如果内存不足，可以减少缓存时间或限制图大小

### Risk 2: 数据一致性

**Risk:** 图缓存可能导致数据不一致（5 分钟延迟）

**Mitigation:**
- 实体变化不频繁（每天最多几次）
- 5 分钟的延迟可以接受
- 可以提供手动刷新接口（force_reload=True）

### Risk 3: 大数据量性能

**Risk:** 如果实体数量增长到 100,000+，NetworkX 性能会下降

**Mitigation:**
- 当前 5,333 节点，NetworkX 完全可以处理
- 如果未来超过 100,000 节点，可以迁移到 Apache AGE 或 Neo4j
- 可以在设计时预留迁移接口

### Trade-off 1: 查询速度 vs 内存占用

**Trade-off:** NetworkX 查询速度快（< 1ms），但内存占用高（520MB）

**Decision:** 选择查询速度，因为：
- 内存占用可以接受（520MB）
- 查询速度是用户体验的关键
- 可以通过缓存策略控制内存占用

### Trade-off 2: 实现复杂度 vs 功能完整性

**Trade-off:** NetworkX 实现简单，但功能不如 Neo4j 完整

**Decision:** 选择实现简单，因为：
- 当前需求不需要高级图算法（社区检测、中心性分析）
- NetworkX 可以满足基本需求（遍历、最短路径）
- 可以后续扩展

## Migration Plan

### Phase 1: 数据库表结构（1 天）
- 创建 entity_embeddings 表
- 创建 entity_graph_cache 表（可选）

### Phase 2: 实体向量嵌入（1 天）
- 实现实体嵌入生成逻辑
- 批量生成实体嵌入
- 存储到 entity_embeddings 表

### Phase 3: NetworkX 图构建（1 天）
- 实现图加载逻辑（从数据库加载节点和边）
- 实现图缓存策略
- 实现图查询接口（BFS、DFS、最短路径）

### Phase 4: MCP 工具集成（1 天）
- 实现 graph_search MCP 工具
- 实现 semantic_search_entities MCP 工具
- 集成到现有系统

### Phase 5: 测试和优化（1 天）
- 单元测试
- 性能测试
- 优化缓存策略

**Total: 5 天**

## Open Questions

1. **是否需要支持增量更新？**
   - 当前方案：每 5 分钟全量重新加载
   - 可选方案：监听数据库变化，增量更新图
   - Decision: 先实现全量加载，后续根据需求决定是否支持增量更新

2. **是否需要支持多语言查询？**
   - 当前方案：使用 name_en + name_zh 生成嵌入
   - 可选方案：为每种语言生成独立的嵌入
   - Decision: 先使用混合嵌入，后续根据需求决定是否支持多语言

3. **是否需要支持图可视化？**
   - 当前方案：不支持
   - 可选方案：集成 pyvis 或 networkx-drawing
   - Decision: 先不支持，后续根据需求决定

4. **是否需要支持分布式部署？**
   - 当前方案：单机部署，内存缓存
   - 可选方案：使用 Redis 缓存图，支持多实例共享
   - Decision: 先实现单机部署，后续根据需求决定是否支持分布式

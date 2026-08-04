## Context

当前 fd-open-data-mcp 的实体同步系统（`entities-data-sync-mechanism`）使用了 PostgreSQL 特有的功能：
- **Advisory Locks** (`pg_advisory_xact_lock`)：用于防止并发同步冲突
- **JSONB 类型**：用于存储元数据（`metadata_json` 列）
- **SERIAL 类型**：自增主键

这些特性在 SQLite 环境中不可用或行为不同，导致同步系统无法在 SQLite 数据库中运行。

## Goals / Non-Goals

**Goals:**
- 让同步系统在 SQLite 和 PostgreSQL 环境中都能正常工作
- 保持向后兼容，不影响现有 PostgreSQL 部署
- 提供自动检测机制，根据数据库类型选择合适实现
- 保持性能可接受（SQLite 环境下的性能降级是预期的）

**Non-Goals:**
- 不改变同步系统的核心逻辑（增量检测、批量操作等）
- 不优化 SQLite 的并发性能（SQLite 本身的限制）
- 不添加新的外部依赖（如 Redis 分布式锁）

## Decisions

### D1. 锁机制：双模式实现

**Decision:** 实现数据库自适应锁机制，PostgreSQL 使用 Advisory Locks，SQLite 使用文件锁。

**Rationale:**
- **Advisory Locks** 是 PostgreSQL 的内存级锁，性能优秀，自动释放
- **文件锁** 在 SQLite 环境中提供基本的并发保护
- 通过数据库 URL 自动检测，无需用户配置

**Implementation:**
```python
# fd_open_data_mcp/sync/locks.py
class SyncLock:
    @staticmethod
    def acquire(session, entity_type, database_url):
        if database_url.startswith("postgresql"):
            # PostgreSQL: Advisory Lock
            session.execute(text(f"SELECT pg_advisory_xact_lock(hashtext('sync:{entity_type}'))"))
        else:
            # SQLite: File Lock
            lock_file = Path("/tmp/entity_sync_lock") / f"{entity_type}.lock"
            lock_file.parent.mkdir(exist_ok=True)
            file_handle = open(lock_file, 'w')
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return file_handle
    
    @staticmethod
    def release(file_handle, database_url):
        if file_handle and not database_url.startswith("postgresql"):
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
            file_handle.close()
```

### D2. JSON 存储：统一为 TEXT

**Decision:** 将 `metadata_json` 列从 JSONB 改为 TEXT，在应用层处理 JSON 序列化/反序列化。

**Rationale:**
- SQLite 不支持 JSONB 类型
- TEXT 类型在两种数据库中都能工作
- 应用层处理 JSON 的开销可接受
- 保持 API 不变（仍然返回 dict）

**Implementation:**
```python
# 写入时
metadata_json = json.dumps(metadata_dict)  # dict -> str
session.execute(text("INSERT INTO ... metadata_json = :metadata"), {"metadata": metadata_json})

# 读取时
metadata_str = row.metadata_json  # str
metadata_dict = json.loads(metadata_str) if metadata_str else {}  # str -> dict
```

### D3. 主键类型：使用 INTEGER

**Decision:** 将 `SERIAL PRIMARY KEY` 改为 `INTEGER PRIMARY KEY AUTOINCREMENT`（SQLite）或保持 `SERIAL`（PostgreSQL）。

**Rationale:**
- SQLite 使用 `AUTOINCREMENT` 关键字
- PostgreSQL 使用 `SERIAL` 类型
- 通过迁移脚本自动处理

**Implementation:**
```sql
-- PostgreSQL
CREATE TABLE entity_sources (
    id SERIAL PRIMARY KEY,
    ...
);

-- SQLite
CREATE TABLE entity_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ...
);
```

### D4. 批量操作：降低批次大小

**Decision:** SQLite 环境下将批量大小从 500 降低到 100。

**Rationale:**
- SQLite 在大量并发写入时容易遇到 "Database is locked" 错误
- 降低批次大小可以减少锁竞争
- PostgreSQL 环境保持 500 批次大小

**Implementation:**
```python
# fd_open_data_mcp/sync/engine.py
BATCH_SIZE = 100 if database_url.startswith("sqlite") else 500
```

### D5. 迁移策略：双脚本方案

**Decision:** 提供两个迁移脚本，一个用于 PostgreSQL，一个用于 SQLite。

**Rationale:**
- 两种数据库的 DDL 语法不同
- 自动检测可能出错，显式选择更安全
- 用户可以根据环境选择合适的脚本

**Implementation:**
- `scripts/migrate_entity_sync_schema_pg.py` - PostgreSQL 版本
- `scripts/migrate_entity_sync_schema_sqlite.py` - SQLite 版本

## Risks / Trade-offs

- **[SQLite 并发限制]** SQLite 在并发写入时性能下降明显 → 建议小型项目（<1000 entities）使用 SQLite，大型项目使用 PostgreSQL
- **[文件锁跨平台问题]** Windows 系统的文件锁机制不同 → 使用 `filelock` 库提供跨平台支持
- **[JSON 查询性能]** TEXT 存储无法使用 PostgreSQL 的 JSONB 索引 → 当前场景下不需要复杂 JSON 查询，性能影响可接受
- **[迁移复杂度增加]** 需要维护两套迁移脚本 → 文档化清楚，用户根据环境选择

## Migration Plan

### Phase 1: 创建锁抽象层
- 实现 `SyncLock` 类，支持 PostgreSQL 和 SQLite
- 修改 `engine.py` 使用新的锁机制

### Phase 2: 修改 JSON 存储
- 将 `metadata_json` 列改为 TEXT 类型
- 修改所有读写代码，添加 JSON 序列化/反序列化

### Phase 3: 创建 SQLite 迁移脚本
- 复制 PostgreSQL 迁移脚本
- 修改 DDL 语法（SERIAL → AUTOINCREMENT, JSONB → TEXT）

### Phase 4: 测试
- 在 SQLite 环境中运行完整测试套件
- 验证锁机制、JSON 存储、批量操作

### Rollback Strategy
如果出现问题：
1. 回滚到 `entities-data-sync-mechanism` 变更后的版本
2. 使用 PostgreSQL 环境继续运行
3. 修复问题后重新部署

## Open Questions

1. **是否需要支持 Windows 环境？** → 当前假设 Unix-like 系统（Linux/macOS），Windows 需要额外测试
2. **是否需要提供迁移工具？** → 从 PostgreSQL 迁移到 SQLite 或反向迁移的工具是否需要？
3. **是否需要监控 SQLite 性能？** → 添加性能指标收集，帮助用户判断是否需要切换到 PostgreSQL

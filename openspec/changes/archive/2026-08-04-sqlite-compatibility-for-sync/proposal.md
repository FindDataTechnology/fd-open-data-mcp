## Why

当前实体同步系统（`entities-data-sync-mechanism`）仅支持 PostgreSQL，使用了 PostgreSQL 特有的特性（Advisory Locks、JSONB 类型）。这导致使用 SQLite 作为数据库的开发环境或小型项目无法使用自动同步功能。

随着系统的发展，越来越多的用户希望在本地开发环境中使用 SQLite 进行快速测试和原型验证。支持 SQLite 兼容性可以降低使用门槛，提升开发体验，同时保持生产环境的 PostgreSQL 性能优势。

## What Changes

- **新增 SQLite 兼容的锁机制**：使用文件锁（`fcntl.flock`）替代 PostgreSQL Advisory Locks，支持 SQLite 环境下的并发控制
- **修改 JSON 存储方式**：将 JSONB 类型改为 TEXT 类型存储 JSON 字符串，在应用层进行序列化和反序列化
- **调整批量操作大小**：针对 SQLite 的并发限制，将批量操作大小从 500 调整为 50-100
- **添加数据库类型检测**：在运行时自动检测数据库类型，选择相应的锁机制和 JSON 处理方式
- **更新迁移脚本**：支持 SQLite 和 PostgreSQL 两种数据库的 schema 创建
- **添加 SQLite 测试场景**：确保同步系统在 SQLite 环境下的功能完整性

## Capabilities

### New Capabilities
- `sqlite-sync-compatibility`: SQLite 环境下的同步兼容性支持，包括文件锁、JSON 文本存储、批量操作优化

### Modified Capabilities
- `entity-sync-engine`: 修改锁机制和 JSON 处理方式以支持双数据库环境

## Impact

- **Affected code**:
  - `fd_open_data_mcp/sync/engine.py` - 添加数据库类型检测和文件锁支持
  - `fd_open_data_mcp/sync/locks.py` (新文件) - 实现统一的锁接口（AdvisoryLock 和 FileLock）
  - `scripts/migrate_entity_sync_schema.py` - 支持 SQLite schema 创建
  - `fd_open_data_mcp/db.py` - 可能需要添加数据库类型检测方法

- **Database schema**:
  - `entity_sources.metadata_json` - PostgreSQL 使用 JSONB，SQLite 使用 TEXT
  - `entity_sync_logs.metadata_json` - 同上
  - 其他表结构保持不变

- **Dependencies**: 无新增依赖（使用 Python 标准库 `fcntl`）

- **Systems**:
  - 本地开发环境可以使用 SQLite 进行同步测试
  - 生产环境继续使用 PostgreSQL 保持高性能
  - 向后兼容：现有 PostgreSQL 部署不受影响

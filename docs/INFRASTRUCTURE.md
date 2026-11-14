# PostgreSQL、RAG 与持久化发送

## 实施基线与边界

目标仓库默认分支为 `main`。本次从 `f83ed3b909121a7ff609c71569ffc99d7a196e6c`
创建功能分支；该提交与 `byteD-x/wechat-bot` 当时的 HEAD 相同，README 和 package.json
也标明该上游。已有 Quart API、Electron、WCFerry、aiosqlite MemoryManager、
SQLite VectorMemory、联系人画像/专属 Prompt、运行期和导出语料 RAG。
没有在此基础上重写直连 OpenAI-compatible / Anthropic 对话。

默认仍使用 SQLite；新服务均需显式配置。Docker 只提供 Web API 与数据服务，
微信连接和定时发送仍必须由 Windows 桌面机器人负责。

## 数据库迁移

失败模式：未知表/列被遗漏、非空目标被覆盖、迁移时写入造成不一致。
因此源文件只读，未知结构拒绝导入，目标必须为空，切换前必须停止所有写入者。
源 SQLite 不被删除，默认 dry-run 不连接 PostgreSQL。

```powershell
python -m pip install -r requirements-infrastructure.txt
python scripts/migrate_postgres.py --memory data/chat_memory.db --vectors data/vector_db/vectors.sqlite3
# 在当前进程环境中设置 WECHAT_BOT_DATABASE_URL（不要把真实 DSN 写进命令历史或提交）
python scripts/migrate_postgres.py --memory data/chat_memory.db --vectors data/vector_db/vectors.sqlite3 --dimensions 1536 --apply
```

向量文件路径请以本机实际文件为准。迁移前停掉机器人、成长服务和 Web API，并用
SQLite backup API 或停写后的完整文件副本保留备份（不要遗漏未 checkpoint 的 WAL）。
`--apply` 先初始化目标空表，然后在单个事务里锁表、导入、核对行数和内容 SHA-256，
再调整自增序列；导入失败回滚目标数据，表结构可能保留。重复导入非空库会拒绝，
不会 truncate 或 upsert 覆盖原数据。向量按 float32 归一后核验，维度不符/非有限数/零向量拒绝。

在 `data/app_config.json` 的 `bot` 中设置 `storage_backend: "postgres"`、
`database_url_env: "WECHAT_BOT_DATABASE_URL"` 和与 embedding 模型匹配的
`embedding_dimensions`，然后完整重启后端。凭据只通过所指环境变量提供。
先用离线检索、消息/画像查询核对，再由维护者手工恢复真实机器人运行。

MemoryManager 的业务接口与 JSON 文本字段保持兼容；PostgreSQL 使用 BIGINT 时间戳与
identity ID，参数绑定转换只覆盖 memory.py 实际使用的 SQL。连接采用语句级 autocommit，
不会让并发协程共享事务；联系人合并等既有多语句操作并非原子事务，维护期间应停写。
向量存储用 JSONB 过滤和 pgvector 余弦距离，保留来源及联系人隔离；当前为精确检索，
未擅自引入 ANN 索引或改变已有排序/引用接口。embedding 模型切换必须新建库并重新索引。

### 备份和回滚

PostgreSQL 模式下，现有文件备份/恢复及 memory/export_rag 清理会明确拒绝，防止误报成功。
使用 `pg_dump`/`pg_restore` 管理数据库，另行备份本地配置、导出语料及其他仍使用 SQLite
的成本/质量数据；Redis 持久卷需要独立备份。恢复须停写，在新库验证后切换连接。
回滚代码前停服务，将 `storage_backend` 改回 `sqlite` 并指向保留的原文件，完整重启。
切换后产生的 PostgreSQL 新数据不会自动反向同步；不能直接丢弃这部分数据。

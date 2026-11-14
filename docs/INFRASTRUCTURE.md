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

## 联系人表达偏好和 LangChain 边界

`POST /api/contact_style` 接受 `chat_id` 和 `style` 对象；`style: null` 清除手工风格。
风格保存于现有 `preferences.personal_style`，与自动推断的 `personality` 和用户编辑的
`contact_prompt` 分开。可设置 `tone`（120 字以内）、`verbosity`（brief/balanced/detailed）、
`emoji_policy`（none/occasional/frequent）、`preferred_phrases`、`avoided_phrases`（各最多 10 条）。
`GET /api/contact_profile?chat_id=...` 返回上述 preferences，prompt snapshot 的摘要包含手工风格。
需启用既有 `personalization_enabled` 才注入画像；全局个人风格仍通过现有 system_prompt 配置。
手工设置不会被后台 personality 推断覆盖，清除也不会删除其他偏好或专属 Prompt。

设置 `bot.rag_retriever_backend: "langchain"` 后，运行期 RAG 和知识来源检索经
`langchain-core` 的 BaseRetriever；默认 `native` 不导入 LangChain。向量库接口、联系人/来源
过滤、混合召回、重排和引用链路保持原样，导出语料风格召回继续由 ExportChatRAG 管理。
这是检索适配，不新增 LangChain LLM/Agent。适配层显式禁用 LangSmith tracing 和外部 callbacks；
不得把聊天内容或凭据上传到观测服务。缺失可选依赖时检索失败，按既有运行时的可选上下文
降级处理；不会悄悄切换数据库或模型。

## Redis Streams 与持久化定时发送

参考 timetask 的“持久化任务 + 轮询到期 + 分派执行”方式实现独立的一次性定时任务，
没有假定仓库已有 timetask 插件，也未复制未知上游。当前不支持 cron/周期任务。

任务正文、收件标识、UTC 时间和状态在 PostgreSQL；Redis 只传 task_id。Windows 机器人
在 `bot.scheduled_messages_enabled: true` 且配置 PostgreSQL/Redis 后启动消费任务。
`scheduled_message_targets` 必须显式列出允许发送的**精确传输目标**；默认空列表。
禁用开关和删除白名单项会在下次领取前生效，不能撤回已经进入发送出口的任务。
首次启用需要完整重启机器人；数据库、维度、Redis 环境变量名称变化需要完整重启后端进程。

API 沿用现有本机访问限制与 API token 校验：

- `POST /api/scheduled_messages`：`{"target":"<已允许的目标>","content":"文本","due_at":<未来的 UTC Unix 秒>,"id":"<可选 UUID>"}`。禁用时返回 409；正文最多 4000 字。只入库，不在请求内发送。
- `GET /api/scheduled_messages`：最近 200 条的 id、时间、状态；不返回正文、收件标识或凭据。
- `DELETE /api/scheduled_messages`：`{"id":"<UUID>"}`；只能取消 pending/queued，领取后的任务不可撤销。

需要重试创建请求时复用同一 UUID；相同内容复用任务，不同内容拒绝覆盖。默认随机 UUID
的两次请求会创建两个任务。发送时复用 WeChatBot.send_text_message 的既有传输和本地落盘行为；
定时任务是显式主动发送，不按自动回复的静默时段/回复策略重新判断，启用前须据此设置白名单。

状态为 pending → queued → sending → sent。取消为 cancelled；停机超过到期后 1 小时的
任务变为 expired，避免恢复时补发陈旧消息。领取用 PostgreSQL 原子条件更新，同一个任务
的重复 Stream 条目不会获得第二次发送权；XACK/XDEL 仅在结果持久化后执行。
Redis 使用 AOF；消费组通过 XAUTOCLAIM 接管超过 5 分钟的 pending entries，queued 任务
也会在 5 分钟后重新投递，以恢复 Redis 丢失的条目。Redis/数据库不可用时本轮暂停，不降级直发。

发送异常、失败回执，或 sending 状态超过 5 分钟均变为 uncertain，**不自动重试**。
微信发送和数据库提交无法跨系统原子化，因此不宣称 exactly-once：进程可能在“领取后发送前”
崩溃而漏发，也可能已经发出但尚未记录成功。uncertain 需维护者核对真实客户端后决定，
不得自动改回 pending。若确需补发，应在人工核对后创建新的任务编号。
当前设计对应单个账号、一个桌面发送器与一个 Redis 消费组；不要把不同账号接入同一任务库。

## Compose 部署

复制 `.env.example` 为本地 `.env`，设置三个不同的长随机 URL-safe 字母数字密码/token。
占位值不应直接使用。`.env`、数据库文件和运行配置均不进入 Git/Docker 构建上下文。

```powershell
docker compose config --quiet
docker compose up -d postgres redis
# 可选的 Web API 容器（不连接微信）
docker compose --profile web up -d --build api
```

PostgreSQL 绑定本机 55432，Redis 绑定本机 56379；都有独立
持久卷。桌面端环境变量应使用 `127.0.0.1` 和这些端口；容器中的地址使用服务名。
密码含保留字符时 DSN 需 URL 编码，示例使用 URL-safe 值避免这个问题。
Web API 容器仅在全新 api_data 卷创建默认配置，不覆盖已有配置；默认发送关闭。
既有 API 的“回环来源 + token”双重限制保持生效，Docker 桥接的宿主机访问会返回 403；
因此该可选 API 容器不发布宿主机端口，只用于容器内回环自检/离线评测。
Electron 和真实微信发送使用本机 Windows 后端，后端连接上述 PostgreSQL/Redis 端口。
该 Compose 是本机开发/单机部署示例，生产部署还需按实际环境设置 TLS、备份及权限。
维护者应预先创建 vector 扩展，再使用权限更小的运行账号。

已有数据迁移不要直接启动默认 API 容器：先按迁移节停写/备份/导入，再配置新的数据卷。
停服务用 `docker compose down`，保留数据卷；不要在已有数据环境执行 `down -v`。

## 验证与运维

离线单元测试可运行：

```powershell
python -m pytest tests/test_storage_backends.py tests/test_contact_style_and_langchain.py tests/test_scheduled_sender.py tests/test_infrastructure_api.py -q
```

真实服务集成测试另设 `TEST_DATABASE_URL`、`TEST_REDIS_URL`，仅允许 localhost/127.0.0.1。
测试创建随机 PostgreSQL schema 和 Redis Stream key，结束只清理自己的对象，不使用
flushdb/flushall。执行 `python -m pytest tests/test_infrastructure_integration.py -q`；未设置变量时
明确 skip，不能将 skip 算作集成通过。CI 的 infrastructure 工作流提供独立临时服务。

上线后的第一小时由部署维护者观察任务 API 的 pending/queued/sending/sent/uncertain/expired
分布，以及 `Scheduled sender paused this tick` 的错误类型（不输出正文或 DSN）。
本项目尚无已配置的生产监控接收人。持续 queued 超过 5 分钟应检查 Redis 和桌面消费者；
sending 超过 5 分钟或出现 uncertain 先停用发送并人工核对。若发现不在白名单的发送、
重复发送或数据核对不一致，立即禁用定时发送、停写，保留 PostgreSQL 和 Redis 供核查；
不要回滚到旧 SQLite 后继续写入而丢弃新数据。本次实际检查结果见 PR 及验证记录。

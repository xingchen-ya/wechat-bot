# 基础设施改造验证记录

日期：2026-11-14。源基线：`f83ed3b909121a7ff609c71569ffc99d7a196e6c`。
环境：Windows、Python 3.13 本地虚拟环境，Docker Desktop 内 PostgreSQL 16 + pgvector、
Redis 7.4；容器镜像 Python 3.10。所有数据为自动生成的测试数据。
没有连接真实微信、真实联系人或外部模型，没有执行用户数据迁移。

| 检查 | 实际结果 |
| --- | --- |
| 最终专项 pytest（storage、contact_style、scheduled_sender、infrastructure_api、bootstrap、integration 和 sender 启动清理） | 27 passed，exit 0；包含真实本地 PostgreSQL/Redis，未 skip |
| 全量 Python 回归（最后两个生命周期修补前） | pytest 输出 605 passed、5 skipped（未设置集成变量）；39.91 秒完成断言，进程在结束阶段未退出，不能标记为干净通过 |
| 生命周期最终补充 | 新增启动失败与取消初始化清理；27 项专项已覆盖；独立审查者另跑 2 项，exit 0 |
| test_bot 单独回归（启动失败修补后、取消修补前） | 输出 37 passed，进程同样在结束阶段未退出 |
| npm test | exit 0，main/renderer 及 backend idle controller 检查通过；未启动 Electron |
| Ruff（改动的 Python 代码、测试及原 CI 文件集） | E4/E7/E9/F 门禁通过；仓库固定此规则集，未大范围格式化旧文件 |
| compileall backend scripts tests | exit 0 |
| 离线 eval smoke | 25 cases、runtime_exception_count=0，exit 0 |
| 离线 eval RAG | 8 cases、runtime_exception_count=0，引用/召回/忠实度/拒答指标均为 1.0，exit 0；仅固定评测集，不代表在线模型表现 |
| Compose config --quiet / infrastructure 镜像构建 | exit 0；未输出本地凭据 |
| 容器内 token 保护的 GET /api/readiness | HTTP 200、deployment_target=web-api；未把模型配置缺失或桌面检查 skipped 描述为机器人可用 |
| 宿主机经 Docker 桥接访问 API | HTTP 403，符合原有回环限制；Compose 因此不发布 API 端口 |
| git diff --check / 新增文件凭据扫描 | 无空白错误；未将本地 .env、测试服务凭据、数据文件或日志加入提交 |

全量测试使用临时网络阻断 fixture，禁止非回环连接。测试进程未退出的原因尚未定位；
已在结果输出后停止本次创建的残留测试进程，不把人工停止算作正常测试退出。
全量最初出现的 5 项失败来自向量初始化改成异步后仍使用同步 MagicMock，已调整为
AsyncMock，随后得到上述 605 passed。专项组可正常退出。

## 数据和故障场景

已检查只读源 dry-run、消息/画像导入、内容校验、自增序列调整、重复导入非空目标拒绝、
后续表含未知列导致整批数据回滚、维度/非有限数/零向量拒绝、联系人和来源隔离、upsert、
手工风格与自动画像分离、LangSmith tracing 环境开启时仍禁止导出、容器配置不覆盖。

任务验证包含重启后恢复、重复消息只领取一次、取消、过期、不确定结果不重试、
Redis 发布失败保留待处理状态、结果落盘失败不 ACK、PostgreSQL 连接关闭后重连且不重发、
启动失败清理、取消慢连接后清理迟到资源并保留 CancelledError。
发送函数均为测试替身；没有用真实账号证明传输成功。

## 审查

整合审查和生命周期补充审查均已完成，无剩余可执行缺陷。审查中发现的持久连接恢复、
初始化取消清理问题已修正并补测试。审查为本地多角度检查，无跨模型或外部代码传输。
简化检查复用了现有业务记忆和关键词排序；提取向量校验/校验和函数、批量写入、
流式迁移和非阻塞向量初始化。没有改变直连聊天链路。

## 尚未验证

- GitHub 托管 CI 结果须以 PR 上实际执行为准，本地记录不替代 CI。
- 真实微信/WCFerry、真实联系人定时发送和在线 LLM/embedding 均未测试。
- 没有生产规模性能压测、实际用户数据库升级或 PostgreSQL 回迁 SQLite 的验收。
- 外部发送无法原子提交数据库状态，不保证 exactly-once；uncertain 必须人工核对。
- Docker API 仅容器内回环使用；桌面端仍依赖 Windows、本机微信与既有权限要求。

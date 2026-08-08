# 06 · Docker、网络与 Hindsight 配置（v4）

> 属于 [Hippocampus 实施计划 v4](00-overview-v4.md) · 署名：Claude · 2026-08-08

## 6.1 一期服务和端口

| 服务 | 网络 | 主机端口 | 说明 |
|---|---|---|---|
| `hippocampus-mcp` | 专属内部网络 + LAN bind | `192.168.2.41:8888 → 8080` | Agent 唯一入口 |
| `hindsight` | 仅内部网络 | 无 | 原生 API/MCP 不发布 |
| `db` | 仅内部网络 | 无 | 5432 不发布 |
| Control Plane | 容器内可存在 | 不发布 9999 | 一期关闭主机访问 |

Hindsight 原生 `/health` 是容器内部端点；Hippocampus 对客户端提供 `/healthz`、`/readyz`、`/version`、`/mcp/`，两组不得混淆。

## 6.2 Compose 依赖与就绪语义

- Hindsight 可依赖 PostgreSQL healthy；Hippocampus MCP **不得**以 `depends_on: hindsight: service_healthy` 阻塞启动；
- MCP 关键依赖仅 Vault、SQLite、秘密扫描器；Hindsight 为可降级索引依赖；
- `healthz` 只判断进程存活；`readyz`：Vault/SQLite/扫描器/audit 不可用→`503`；Hindsight 不可用→`200` + `degraded/index_backend_unavailable`；
- 健康响应只含状态与计数，不含配置、路径、模型、Token 或错误正文。

## 6.3 镜像与版本

- `ghcr.io/vectorize-io/hindsight:0.9.0`：Phase 0 验证 `linux/arm64` manifest 后固定 digest；
- pgvector 镜像固定支持 arm64 的明确 digest，不长期用浮动 `pg18` 标签；
- Hippocampus MCP 镜像固定源码 commit 与本地构建 digest；禁止 `latest` 进正式 `main`；
- 升级 Hindsight/embedding/reranker/检索后端必须先在临时 Bank/实例验证。

## 6.4 Hindsight 配置（候选模板，Phase 0 实测准入）

非秘密固定项：

```bash
HINDSIGHT_API_STORE_DOCUMENT_TEXT=false
HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION=false
HINDSIGHT_API_WORKER_ID=pi5-hindsight-main
HINDSIGHT_API_MCP_ENABLED=false
HINDSIGHT_API_LLM_TRACE_ENABLED=false
HINDSIGHT_API_LLM_DEBUG_DUMP_4XX=false
HINDSIGHT_API_AUDIT_LOG_ENABLED=false
# D3：telemetry/trace exporter 关闭。确切变量名（OTEL/trace 相关，如 OTEL_SDK_DISABLED=true
# 或 HINDSIGHT_API_OTEL_* 开关）由 Phase 0 对 v0.9.0 源码核对后固定，并纳入验收（09 §10.1）。
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension
HINDSIGHT_API_LLM_OUTPUT_LANGUAGE=Chinese
HINDSIGHT_API_TEXT_SEARCH_EXTENSION_NATIVE_LANGUAGE=simple
HINDSIGHT_API_QUERY_ANALYZER_LANGUAGES=en,zh
```

一期 retain + failover 候选。以下只放非秘密 provider/model/strategy；base URL 与每个成员的 API key 由 [04 §4.4](04-security.md) 的 Compose 显式映射注入。编号成员不继承主成员 key：

```bash
# retain/全局：轻量档 + 回退
HINDSIGHT_API_LLM_PROVIDER=openai
HINDSIGHT_API_LLM_MODEL=gemini-3-flash
HINDSIGHT_API_LLM_1_PROVIDER=openai
HINDSIGHT_API_LLM_1_MODEL=deepseek-v4-flash
HINDSIGHT_API_LLM_STRATEGY={"mode":"failover"}
HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS=16000

# 多语言检索（维度入库后锁死，先临时库验证）
HINDSIGHT_API_EMBEDDINGS_PROVIDER=local
HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
HINDSIGHT_API_RERANKER_PROVIDER=local
HINDSIGHT_API_RERANKER_LOCAL_MODEL=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
HINDSIGHT_API_RERANKER_1_PROVIDER=rrf
HINDSIGHT_API_RERANKER_MAX_CANDIDATES=100
```

准入条件：

| 角色 | 候选 | 进入正式配置的条件 |
|---|---|---|
| retain 主/备 | `gemini-3-flash` / `deepseek-v4-flash` | chat、structured output、retain 实测通过；人为失败后备真实接管 |
| 本地 embedding | `paraphrase-multilingual-MiniLM-L12-v2` | ARM64 启动、内存与中文召回通过 |
| 本地 reranker + fallback | `mmarco-mMiniLMv2-L12-H384-v1` → `rrf` | ARM64 启动、Top 5 测试通过；故障兜底可用 |

关键说明：

- `STORE_DOCUMENT_TEXT` 和 Hindsight 自带 `AUDIT_LOG` 都可被 Bank 配置影响：`main` 必须显式设 `store_document_text=false`、`audit_log_enabled=false` 并在启动时读取 config 复核；
- 该选项只防持久化，不改变「内容会被发送给 Hindsight/LLM」；retain payload 只能是已扫描 retrieval_text；
- v0.9.0 默认启用 LLM request trace，会把 prompt/output 写入 PostgreSQL；一期必须显式关闭。[09 §10.1](09-acceptance-gonogo.md) 必须验证 `llm_requests` 和 Hindsight audit 表无请求/响应副本，4xx/timeout/Docker 日志也无精确 payload；
- `simple` 只是兼容基线；一期以真实中文召回集验证，不足则记录为 Phase 3 PGroonga/VectorChord 评估项；
- 模型字符串与 cliproxy 兼容性一律 Phase 0 实测，文档示例不构成可用性证明；
- reflect 一期既不暴露也不作为 Go/No-Go 依赖；`gpt-5.5/glm-5.2` 等候选只在 Phase 3 获得新授权后再配置和验证。

## 6.5 运行资源初始上限

Hindsight 3 GiB / PostgreSQL 1 GiB / Hippocampus MCP+worker 512 MiB；不创建 swapfile；HF cache 专属 named volume；不与现有 Home Assistant、Gotify、Backup API、Caddy 共用 Compose project/network。出现 OOM、持续热降频或现有业务抖动时停止扩容与导入，先复核模型与资源。

---

*署名：Claude*

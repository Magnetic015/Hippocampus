# 06 · Docker、网络与 Hindsight 配置（v4.1）

> 属于 [Hippocampus 实施计划 v4.1](00-overview-v4.md) · 修订：Codex · 2026-08-08

## 6.1 一期服务和端口

| 服务 | 网络 | 主机端口 | 说明 |
|---|---|---|---|
| `hippocampus-mcp` | 专属内部网络 + LAN bind | `192.168.2.41:8888 → 8080` | Agent 唯一入口 |
| `hindsight` | 仅内部网络 | 无 | 原生 API/MCP 不发布 |
| `db` | 仅内部网络 | 无 | 5432 不发布 |
| Control Plane | **不启动** | 无，容器内外均无 9999 listener | `HINDSIGHT_ENABLE_CP=false` |

Hindsight 原生 `/health` 是容器内部端点；Hippocampus 对客户端提供 `/healthz`、`/readyz`、`/version`、`/mcp/`，两组不得混淆。

## 6.2 Compose 身份、网络、挂载与加固

固定 inventory：

| 对象 | 固定值 | 约束 |
|---|---|---|
| Compose project | `hippocampus` | 所有命令显式 `docker compose -p hippocampus --env-file /home/kkp/.config/hippocampus/hippocampus.env ...` |
| services | `hippocampus-mcp`, `hindsight`, `db` | 预期容器名由 project+service 派生；禁止 `container_name` 与现网抢名 |
| backend network | `hippocampus-backend` | 无主机发布的普通 bridge，`internal:false`；允许 Hindsight 到指定 cliproxy/HF 出站，不等于 Docker `internal:true` |
| PostgreSQL volume | `hippocampus-pgdata:/var/lib/postgresql/18/docker` | 仅 pg18 db 挂载，回滚默认保留；镜像版本变化先重验 target |
| HF/cache volume | `hippocampus-hf-cache:/home/hindsight/.cache` | 仅 Hindsight 挂载；覆盖 HF/tiktoken/cache 写入，回滚默认保留 |
| Vault bind | `/home/kkp/hippocampus/vault:/data/vault` | 仅 MCP/worker 读写，Hindsight/db 不挂载 |
| state bind | `/home/kkp/hippocampus/state:/data/state` | 仅 MCP/worker 读写，0700/0600 |

- 只有 MCP 发布 `192.168.2.41:8888:8080`；Hindsight API、PostgreSQL 和 9999 均无 `ports`/`expose` 给主机；
- Hindsight 需要 cliproxy/HF 出站，因此 backend 不设 `internal:true`；主机暴露与出站控制分开验收；
- project/network/volume/service 均加 `io.hippocampus.managed=true`、`io.hippocampus.plan=v4.1` 标签，回滚先按 project 与标签双重核对；
- MCP 用 Phase 1 只读取得并固定的 `kkp` 数字 UID:GID，以访问其 0700 bind；Hindsight v0.9.0 候选固定 `1000:1000`，Phase 0 必须按镜像 digest 验证；db 使用镜像原生非 root UID/GID并记录。任一不符即停止，不用 root/chmod 兜底；
- MCP/Hindsight 使用 `no-new-privileges:true`、`init:true`、只读 rootfs、上述精确 writable mount 与 `/tmp` 有界 `tmpfs`；默认 `cap_drop: [ALL]`，Phase 0 若镜像确需能力必须停止并给出最小能力和证据，不能直接放宽；
- db 使用固定 pg18 digest 与专属 pgdata；不挂 Vault/state/HF cache；三服务 `restart: unless-stopped`，Hindsight `stop_grace_period: 30s`，MCP/db 至少 10s；
- 三容器均禁止 privileged、host network/PID/IPC、Docker socket/管理 API、非必要 device；日志驱动限制大小/文件数，且应用层禁止 payload/Header/异常正文；
- MCP/worker 和 db 的停止/恢复语义、Compose down/up 后 Bank/Outbox/Vault 持久性在 Phase 0 验证；
- healthcheck：db 用 `pg_isready -U hippocampus -d hindsight`；Hindsight 用容器内 `/health`；MCP 从 loopback 调 `/healthz`，该 loopback 只允许健康路由，不能访问 `/mcp/`；`/readyz` 另按依赖语义。Hindsight 只依赖 db healthy，MCP 不硬依赖 Hindsight healthy；
- 渲染后的 Compose 必须以 allowlist 方式核对 image digest、user、mount、network、ports、capability、security_opt、healthcheck、restart、resources 与 env key 名。runtime 只允许 `docker inspect --format` 读取这些非秘密字段；禁止未过滤 inspect、`.Config.Env`、`config --environment` 或任何实际秘密值。

## 6.3 Compose 依赖与就绪语义

- Hindsight 可依赖 PostgreSQL healthy；Hippocampus MCP **不得**以 `depends_on: hindsight: service_healthy` 阻塞启动；
- MCP 关键依赖仅 Vault、SQLite、秘密扫描器；Hindsight 为可降级索引依赖；
- `healthz` 只判断进程存活；`readyz`：Vault/SQLite/扫描器/audit 不可用或出现 `DURABILITY_GAP/STATE_INVARIANT_BROKEN`→`503`；Hindsight 不可用→`200` + `degraded/index_backend_unavailable`；
- 健康响应只含状态与计数，不含配置、路径、模型、Token 或错误正文。

## 6.4 镜像与版本

- `ghcr.io/vectorize-io/hindsight:0.9.0`：Phase 0 验证 `linux/arm64` manifest 后固定 digest；
- pgvector 镜像固定支持 arm64 的明确 digest，不长期用浮动 `pg18` 标签；
- Hippocampus MCP 镜像固定源码 commit 与本地构建 digest；禁止 `latest` 进正式 `main`；
- 升级 Hindsight/embedding/reranker/检索后端必须先在临时 Bank/实例验证。

## 6.5 Hindsight 配置（候选模板，Phase 0 实测准入）

非秘密固定项：

```bash
HINDSIGHT_API_STORE_DOCUMENT_TEXT=false
HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION=false
HINDSIGHT_API_WORKER_ID=pi5-hindsight-main
HINDSIGHT_API_MCP_ENABLED=false
HINDSIGHT_ENABLE_API=true
HINDSIGHT_ENABLE_CP=false
HINDSIGHT_API_LLM_TRACE_ENABLED=false
HINDSIGHT_API_OTEL_TRACES_ENABLED=false
HINDSIGHT_API_LLM_DEBUG_DUMP_4XX=false
HINDSIGHT_API_AUDIT_LOG_ENABLED=false
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
- `HINDSIGHT_API_OTEL_TRACES_ENABLED=false` 必须显式存在；Hindsight v0.9.0 的 `HINDSIGHT_API_OTEL_EXPORTER_OTLP_ENDPOINT`、`HINDSIGHT_API_OTEL_EXPORTER_OTLP_HEADERS`，以及通用 `OTEL_EXPORTER_OTLP_ENDPOINT/HEADERS` 与其他 exporter endpoint/credential 均不得出现在 service env。运行时验证无 exporter、span/event 或 trace 副本；
- `HINDSIGHT_ENABLE_CP=false` 必须使 standalone image 不启动 Control Plane；容器内 `ss` 无 9999 listener，从 MCP 容器连接 9999 也必须失败；
- `simple` 只是兼容基线；一期以真实中文召回集验证，不足则记录为 Phase 3 PGroonga/VectorChord 评估项；
- `CLIPROXY_OPENAI_BASE_URL` 必须是已规范化、包含且只包含一次 `/v1` 的完整 base URL；不得在 Compose/代码追加路径。模型字符串与 cliproxy 兼容性一律 Phase 0 实测，文档示例不构成可用性证明；
- reflect 一期既不暴露也不作为 Go/No-Go 依赖；`gpt-5.5/glm-5.2` 等候选只在 Phase 3 获得新授权后再配置和验证。

## 6.6 运行资源初始上限

Hindsight 3 GiB / PostgreSQL 1 GiB / Hippocampus MCP+worker 512 MiB，必须落实为 Compose 可验证的硬上限而非仅文档目标；不创建 swapfile；HF cache 使用固定专属 named volume；不与现有 Home Assistant、Gotify、Backup API、Caddy 共用 project/network。出现 OOM、持续热降频或现有业务抖动时停止扩容与导入，先复核模型与资源。

---

*修订：Codex*

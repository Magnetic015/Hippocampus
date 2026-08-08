# 04 · 秘密检测、读取安全与运行 secret plane（v4.1）

> 属于 [Hippocampus 实施计划 v4.1](00-overview-v4.md) · 修订：Codex · 2026-08-08

## 4.1 服务端强制扫描

以下 gate 只覆盖 `/mcp/` 数据协议。`/healthz`、`/readyz`、`/version` 走独立无正文路由：容器 healthcheck 只允许 `127.0.0.1/::1`，已启用客户端只允许并集 gate 内地址；loopback 例外永远不能访问 `/mcp/`。固定处理顺序：

```text
HTTP access logger 禁止记录 Header、query string 和 body，并生成 request_id
→ 仅按 socket peer 校验全部已启用 client 精确地址的非空并集 gate；忽略 X-Forwarded-For
→ auth middleware 消费 Authorization，以常量时间校验 Token
→ 立即丢弃原始 Header，只向业务层传 client_id/permissions
→ 成功认证后立即插入最小 audit(request_id, client_id, ts, state=started)；operation_enum/tool_enum 均为 NULL
→ 再查询 client_sources 校验该 client_id 与 socket peer 的有效绑定；不匹配固定拒绝 source_mismatch
→ 有界解析并扫描 JSON-RPC method/id/tool name；只映射 allowlisted operation_enum，tools/call 再映射四个 canonical tool_enum 或 invalid_tool
→ 业务 schema 规范化与 registry 授权
→ 本地确定性秘密扫描全部业务字段
→ audit 只以安全枚举/服务端字段路径更新决策结果
→ 才允许 Vault/Outbox/Hindsight 操作
```

并集 gate 拒绝发生在读取 Authorization 之前，只记录服务端 request_id、socket peer 与固定 `source_denied`，不得读取或记录凭证；通过并集 gate 但 client→peer 不匹配时，在既有最小 audit 行只记 `source_mismatch`。audit insert 失败时不得继续 client 绑定、JSON-RPC envelope、业务 schema/授权/扫描、文件读写或 Hindsight 调用。客户端提供的 request-id Header 与 JSON-RPC `id` 都不是服务端审计 ID；前者忽略，后者仅在通过有界校验和秘密扫描后按协议瞬时回显，永不持久化。batch、压缩 body、未知 Content-Type 和超限 envelope 在 [07 §7.0](07-mcp-tools-audit.md) 整体拒绝。

扫描范围至少包括：

- `idempotency_key`、title、summary、retrieval_text、detail_body、event_at、project、type、metadata、filters、status 查询键、URI 和文件名；
- JSON-RPC 中有界解析出的 `method`、客户端 `id`、`tools/call.params.name` 与未知键；这些原值只参与拒绝判断，不进入 audit、日志或业务存储；
- `memory_search` query 及过滤参数；
- 渲染后的完整 Markdown、worker 精确 retain body、导入/reindex/reconcile/retry 输入；
- 外部修改、策略升级或 SHA 不匹配后的 `memory_read`；
- Hindsight 返回值的字段 allowlist、URI/标签一致性检查和最终本地秘密扫描。

`idempotency_key` 只接受 UUIDv4；project/type/status key 等先做严格 schema 拒绝，再扫描。审计只允许固定 `operation_enum`；仅 tools/call 允许四个 canonical `tool_enum`，未知/畸形工具名称固定为 `invalid_tool`。字段路径只允许服务端 schema 枚举，未知键固定为 `unknown_field`。扫描器必须是 Hippocampus 本地、确定性的服务端组件；客户端预检和 Hindsight 自身防御只能作为纵深。

合法 MCP `Authorization` Header 是瞬时认证凭证，不是候选记忆内容。合法 Header 本身不能触发内容扫描拒绝；同一 Token 一旦被复制到 query/title/正文/metadata 等业务字段则必须拒绝。无效 Header 不得进入 hash、异常对象、access log 或 audit。

## 4.2 拒绝语义

命中秘密：整笔拒绝；不自动脱敏保存；不留隔离副本；秘密值、片段、摘要、长度、明文 hash、payload fingerprint 和请求 payload 不进入 Vault、staging、SQLite、Outbox、审计、Hindsight、PostgreSQL、队列、死信、日志、trace、pcap/raw capture/proxy dump 或 `backups-test`。允许且必须更新一条**脱敏决策审计**，只含 request_id、client_id、allowlisted operation enum、canonical tool enum（tools/call 未解析则 `invalid_tool`）、`outcome=secret_rejected`、规则类别和服务端枚举字段路径（未知键为 `unknown_field`）；不得记录命中值、原始键名、query hash/长度或异常原文。HTTP/JSON-RPC 错误响应同样不得回显命中材料；疑似秘密或非法 JSON-RPC id 返回 `id:null`。

```json
{
  "code": "HIPPOCAMPUS_SECRET_REJECTED",
  "stored": false,
  "indexed": false,
  "retryable": false,
  "fields": ["detail_body"],
  "categories": ["credential"],
  "message": "拒绝写入：请删除实际秘密值，仅保留占位符或外部秘密引用。"
}
```

扫描器不可用/超时/异常：`HIPPOCAMPUS_SECRET_SCAN_UNAVAILABLE`，四个数据工具及 worker/retry/recovery 全部 fail-closed；已认证调用只把该安全错误码更新到既有最小 audit 行。

## 4.3 外部修改和遗留内容

- Vault 只允许 Hippocampus 容器内固定非 root UID/GID 写入；不新增主机账户，Agent、Hindsight、OpenClaw 不得直接挂载或写入；
- worker 外发前核对 URI、真实路径、symlink、期望 SHA 与扫描策略版本；
- `memory_read` 核对当前文件 SHA 与最后通过扫描的 SHA，不一致先重扫；
- 遗留文件命中秘密标记 `policy_blocked`，停止读取与索引；只记录文档 ID、规则类别与状态；
- 人工流程负责撤销/轮换与安全清理，不得把原文移入"隔离目录"。

## 4.4 运行 secret plane

- 路径 `/home/kkp/.config/hippocampus/`，目录 `0700`、文件 `0600`、`umask 077`；
- 不入 Vault、state、Outbox、Git、日志、审计或 `backups-test`；`.gitignore` 排除 `.env`、`secrets/`、Token 文件；
- `docker compose --env-file /home/kkp/.config/hippocampus/hippocampus.env up -d` 中，`--env-file` **只是 Compose 插值源，不会自动把全部变量注入容器**；
- 项目内非秘密 `hindsight.env` **必须**作为 Hindsight service 的 `env_file` 挂接；所有秘密必须在 `compose.yaml` 对各 service 逐项显式映射，未提供时用 `:?required` 启动失败：

```yaml
services:
  db:
    environment:
      POSTGRES_USER: hippocampus
      POSTGRES_DB: hindsight
      POSTGRES_PASSWORD: ${HIPPOCAMPUS_DB_PASSWORD:?required}
  hindsight:
    env_file:
      - ./hindsight.env
    environment:
      HINDSIGHT_API_DATABASE_URL: "postgresql://hippocampus:${HIPPOCAMPUS_DB_PASSWORD:?required}@db:5432/hindsight"
      HINDSIGHT_API_TENANT_API_KEY: ${HINDSIGHT_INTERNAL_TOKEN:?required}
      HINDSIGHT_API_LLM_BASE_URL: ${CLIPROXY_OPENAI_BASE_URL:?required}
      HINDSIGHT_API_LLM_API_KEY: ${CLIPROXY_KEY:?required}
      HINDSIGHT_API_LLM_1_BASE_URL: ${CLIPROXY_OPENAI_BASE_URL:?required}
      HINDSIGHT_API_LLM_1_API_KEY: ${CLIPROXY_KEY:?required}
  hippocampus-mcp:
    environment:
      HIPPOCAMPUS_HINDSIGHT_TOKEN: ${HINDSIGHT_INTERNAL_TOKEN:?required}
      HIPPOCAMPUS_TOKEN_PEPPER: ${HIPPOCAMPUS_TOKEN_PEPPER:?required}
      HIPPOCAMPUS_AUDIT_HMAC_KEY: ${HIPPOCAMPUS_AUDIT_HMAC_KEY:?required}
      HIPPOCAMPUS_IDEMPOTENCY_HMAC_KEY: ${HIPPOCAMPUS_IDEMPOTENCY_HMAC_KEY:?required}
```

- 数据库密码必须是至少 256-bit 的 URL-safe 随机值，保证直接用于内部 DSN 时不发生 URL 解析歧义；
- `HINDSIGHT_INTERNAL_TOKEN`、`CLIPROXY_KEY`、数据库密码、Token pepper、audit HMAC key、idempotency HMAC key 和三个客户端 Token 均不得复用；
- `CLIPROXY_OPENAI_BASE_URL` 是完整 OpenAI-compatible base URL（含且只含一次 `/v1`）；启动前要求 `http|https`、无 userinfo/query/fragment、无尾斜杠，禁止代码再次拼接 `/v1`；
- 每个容器只获得自身需要的秘密；用 `docker compose config -q` 和只报告"缺失变量名"的容器探针验证。禁止打印/保存 `config --environment`、未过滤 `docker inspect`、`.Config.Env`、进程环境或调试输出；仅允许 `docker inspect --format` 读取 [06](06-deployment.md) 明列的非秘密 runtime 字段；
- 明文客户端 Token 仅通过一次性 `0600` 文件或 OS 凭证存储交付一次，不进 stdout/argv；交付确认后删除 Pi5 上的一次性明文副本。
- root、Docker daemon 和经用户批准的 Docker 管理员属于明确受信边界，技术上可读取容器环境。秘密生成或注入前必须只读核对 Docker socket owner/group/ACL、`docker` group、context、`DOCKER_HOST`、TCP daemon 与管理 API；出现未批准主体立即停止。
- 三个容器均不得挂 Docker socket/等价管理 API，不得使用 privileged、host PID/IPC/network 或未声明 device。Hippocampus/Hindsight 固定非 root UID/GID、`no-new-privileges`、按 Phase 0 验证后的最小 capability、只读 rootfs 与精确 writable mount/tmpfs；完整合同见 [06](06-deployment.md)。

## 4.5 Client registry 与统一授权

registry 位于 SQLite state 中，只保存：

```text
clients(client_id, token_hash, token_version, source_tag,
permissions, readable_projects, writable_projects, allowed_types,
expires_at, revoked_at, created_at, last_seen_at)
client_sources(client_id FK, canonical_ip, address_family,
verified_at, revoked_at, PRIMARY KEY(client_id,canonical_ip))
```

- 三个客户端 Token 各自至少 256-bit 随机，变量名和值均不同；Token hash 使用 HMAC-SHA-256 + 独立 runtime pepper，常量时间比较；
- `mac-claude`、`mac-codex`、`dockerNode-openclaw` 各有独立有效 `client_sources`、权限、read/write project allowlist、type allowlist、到期时间和撤销状态；地址值只允许规范化单 IP，不允许 CIDR/通配/hostname；`global` 与 `commissioning` 都必须在 bootstrap manifest 中显式列出，系统不做隐式授予；
- 四个 MCP 工具共用同一授权函数：search/read/status 的 project 必须属于 `readable_projects`；commit 必须属于 `writable_projects`；filter 只能缩小权限；read/status 从 URI/document 映射回 project 后再次授权；
- Hindsight `tag_groups` 只负责数据过滤，不能替代 registry 授权；返回结果还要按 metadata/URI/project 做服务端后置校验；
- `trust:curated` 只能由独立受控导入身份生成，普通三客户端永远只能生成 `trust:agent`。
- registry 与 `client_sources` 生命周期只由非 MCP 管理 CLI 操作：bootstrap/issue/rotate 从 stdin 或一次性 `0600` 文件接收秘密，绝不经 argv/stdout；bind-peer/revoke-peer 事务化更新精确地址；revoke 只需 client_id；list/status 只输出安全元数据，不输出 token hash。并集 gate 每次请求从有效 `client_sources` 的索引快照读取，SQLite 故障即 fail-closed，不依赖易失手工 reload。

## 4.6 一期 LAN HTTP 风险

一期因明确排除 Caddy、证书和公网改动，示例仍使用 LAN HTTP。Bearer Token 在同网段可被被动抓包，三独立 Token 不能消除此风险，因此：

- Phase 0 只读形成 Mac 候选 source；Phase 2 接入 `.2.3` 前再只读形成其候选。每个客户端启用前必须把现场确认的 socket peer 精确地址绑定到该 `client_id`；本机 Claude/Codex 可共享一个源 IP，但身份仍按 Token 分离；直连场景忽略 `X-Forwarded-For`；
- listener 启动前先加载全部已启用 client 地址的非空精确并集 gate；认证后再做 client→peer 绑定校验。空集合、CIDR/网段、通配、hostname 和自动学习均禁止。候选与实际 peer 不一致时停止，不得自动扩容；source 变化需重新确认、更新对应 client 并轮换其 Token；
- PoC Token 必须短期有效、可单独撤销，并在验收结束后统一轮换；
- Vault 永久禁止秘密，不能把"没有秘密正文"误当作 Token 安全；
- 三客户端开始长期正式写入前，Go/No-Go 必须二选一：另行授权启用不依赖 Caddy 的内部 TLS 并完成三端信任验证，或由用户明确接受受信 LAN 明文 Bearer 的残余风险；本计划不把 TLS 变更自动并入一期；
- Phase 4 远程接入一律强制 TLS，不继承本项例外。

---

*修订：Codex*

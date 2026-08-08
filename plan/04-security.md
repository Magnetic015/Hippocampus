# 04 · 秘密检测、读取安全与运行 secret plane（v4）

> 属于 [Hippocampus 实施计划 v4](00-overview-v4.md) · 署名：Claude · 2026-08-08

## 4.1 服务端强制扫描

固定处理顺序：

```text
HTTP access logger 禁止记录 Header、query string 和 body，并生成 request_id
→ auth middleware 消费 Authorization，以常量时间校验 Token
→ 立即丢弃原始 Header，只向业务层传 client_id/permissions
→ 成功认证后立即插入无 payload 的最小 audit 行（request_id UNIQUE, state=started）；
  audit 不可写则在任何校验/文件/Hindsight 操作前 fail-closed（C15）
→ schema 规范化与 registry 授权（拒绝时更新同一 audit 行为脱敏终态，前置拒绝不漏审计）
→ 本地确定性秘密扫描全部业务字段
→ audit 更新决策结果（不含 payload）
→ 才允许 Vault/Outbox/Hindsight 操作
```

扫描范围至少包括：

- `idempotency_key`、title、summary、retrieval_text、detail_body、event_at、project、type、metadata、filters、status 查询键、URI 和文件名；
- `memory_search` query 及过滤参数；
- 渲染后的完整 Markdown、worker 精确 retain body、导入/reindex/reconcile/retry 输入；
- 外部修改、策略升级或 SHA 不匹配后的 `memory_read`；
- Hindsight 返回值的字段 allowlist、URI/标签一致性检查和最终本地秘密扫描。

`idempotency_key` 只接受 UUIDv4；project/type/status key 等先做严格 schema 拒绝，再扫描。扫描器必须是 Hippocampus 本地、确定性的服务端组件；客户端预检和 Hindsight 自身防御只能作为纵深。

合法 MCP `Authorization` Header 是瞬时认证凭证，不是候选记忆内容。合法 Header 本身不能触发内容扫描拒绝；同一 Token 一旦被复制到 query/title/正文/metadata 等业务字段则必须拒绝。无效 Header 不得进入 hash、异常对象、access log 或 audit。

## 4.2 拒绝语义

命中秘密：整笔拒绝；不自动脱敏保存；不留隔离副本；秘密值、片段、摘要、长度、明文 hash 和请求 payload 不进入 Vault、staging、SQLite、Outbox、审计、Hindsight、PostgreSQL、队列、死信、日志、trace 或 `backups-test`。允许且必须写一条**脱敏决策审计**，只含 request_id、client_id、tool、`outcome=secret_rejected`、规则类别和字段路径；不得记录命中值、query hash/长度或异常原文。错误响应同样不回显命中材料。

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

扫描器不可用/超时/异常：`HIPPOCAMPUS_SECRET_SCAN_UNAVAILABLE`，fail-closed。

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
- 项目内非秘密 `hindsight.env` 可作为只读 service env file；所有秘密必须在 `compose.yaml` 对各 service 逐项显式映射，未提供时用 `:?required` 启动失败：

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
      HINDSIGHT_API_DATABASE_URL: postgresql://hippocampus:${HIPPOCAMPUS_DB_PASSWORD:?required}@db:5432/hindsight
      HINDSIGHT_API_TENANT_API_KEY: ${HINDSIGHT_INTERNAL_TOKEN:?required}
      HINDSIGHT_API_LLM_BASE_URL: ${CLIPROXY_BASE_URL:?required}/v1
      HINDSIGHT_API_LLM_API_KEY: ${CLIPROXY_KEY:?required}
      HINDSIGHT_API_LLM_1_BASE_URL: ${CLIPROXY_BASE_URL:?required}/v1
      HINDSIGHT_API_LLM_1_API_KEY: ${CLIPROXY_KEY:?required}
  hippocampus-mcp:
    environment:
      HIPPOCAMPUS_HINDSIGHT_TOKEN: ${HINDSIGHT_INTERNAL_TOKEN:?required}
      HIPPOCAMPUS_TOKEN_PEPPER: ${HIPPOCAMPUS_TOKEN_PEPPER:?required}
      HIPPOCAMPUS_AUDIT_HMAC_KEY: ${HIPPOCAMPUS_AUDIT_HMAC_KEY:?required}
```

- 数据库密码必须是至少 256-bit 的 URL-safe 随机值，保证直接用于内部 DSN 时不发生 URL 解析歧义；
- `HINDSIGHT_INTERNAL_TOKEN`、`CLIPROXY_KEY`、数据库密码、Token pepper、audit HMAC key 和三个客户端 Token 均不得复用；
- 每个容器只获得自身需要的秘密；用 `docker compose config -q` 和只报告"缺失变量名"的容器探针验证，禁止打印/保存 `config --environment`、`docker inspect`、进程环境或调试输出；
- 明文客户端 Token 仅通过一次性 `0600` 文件或 OS 凭证存储交付一次，不进 stdout/argv；交付确认后删除 Pi5 上的一次性明文副本。

## 4.5 Client registry 与统一授权

registry 位于 SQLite state 中，只保存：

```text
client_id, token_hash, token_version, source_tag,
permissions, allowed_projects, allowed_types,
expires_at, revoked_at, created_at, last_seen_at
```

- 三个客户端 Token 各自至少 256-bit 随机，变量名和值均不同；Token hash 使用 HMAC-SHA-256 + 独立 runtime pepper，常量时间比较；
- `mac-claude`、`mac-codex`、`dockerNode-openclaw` 各有独立权限、project/type allowlist、到期时间和撤销状态；三者 `allowed_projects` 默认包含 `global`（D6）；
- 四个 MCP 工具共用同一授权函数：search 的 project 必须属于 `allowed_projects`；filter 只能缩小权限；read/status 必须从 URI/document 映射回 project 后再次授权；
- Hindsight `tag_groups` 只负责数据过滤，不能替代 registry 授权；返回结果还要按 metadata/URI/project 做服务端后置校验；
- `trust:curated` 只能由独立受控导入身份生成，普通三客户端永远只能生成 `trust:agent`。

## 4.6 一期 LAN HTTP 风险

一期因明确排除 Caddy、证书和公网改动，示例仍使用 LAN HTTP。Bearer Token 在同网段可被被动抓包，三独立 Token 不能消除此风险，因此：

- Phase 1 仅允许受信 LAN PoC，Hippocampus 应用层只接受 Phase 0 记录的精确客户端源地址集合（本机 Claude/Codex 可共享一个源 IP，但身份仍按 Token 分离）；直连场景忽略 `X-Forwarded-For`；
- PoC Token 必须短期有效、可单独撤销，并在验收结束后统一轮换；
- Vault 永久禁止秘密，不能把"没有秘密正文"误当作 Token 安全；
- 三客户端开始长期正式写入前，Go/No-Go 必须二选一：另行授权启用不依赖 Caddy 的内部 TLS 并完成三端信任验证，或由用户明确接受受信 LAN 明文 Bearer 的残余风险；本计划不把 TLS 变更自动并入一期；
- Phase 4 远程接入一律强制 TLS，不继承本项例外。

---

*署名：Claude*

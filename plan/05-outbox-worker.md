# 05 · Outbox 与一致性定稿（v4）

> 属于 [Hippocampus 实施计划 v4](00-overview-v4.md) · 署名：Claude · 2026-08-08

## 5.1 SQLite 边界

- 文件 `/home/kkp/hippocampus/state/outbox.db`（state+registry+outbox+audit 同库分表）；state 目录 `0700`，主库/WAL/SHM `0600`（`umask 077`）；`journal_mode=WAL`、`synchronous=FULL`；仅本地文件系统，不放 NFS/SMB；
- Outbox 不保存 title、summary、retrieval_text、正文、Header、LLM 请求/响应或异常原文；
- 字段：`event_id, request_id, client_id, idempotency_key, document_id, uri, event_type, desired_sha256, previous_sha256, scan_policy_version, status, attempt, next_attempt_at, lease_owner, lease_expires_at, error_code, created_at, updated_at`；
- 唯一约束：`(client_id, idempotency_key)`、`(document_id, desired_sha256, event_type)`；
- 普通 Agent 的 `idempotency_key` 只允许 UUIDv4；`document_id/uri/request_id` 全由服务端生成，客户端提交这些保留字段直接拒绝；
- `error_code` 仅脱敏枚举（`HINDSIGHT_TIMEOUT`、`UPSTREAM_5XX`、`HASH_MISMATCH` 等），不存 traceback 或响应正文。

## 5.2 主写链路（可恢复状态机）

```text
1. 认证通过后立即插入 audit(request_id UNIQUE, state=started)；失败则在任何校验/文件操作前拒绝（C15）
2. 按 04 §4.1 完成 schema、统一授权和业务字段秘密扫描；拒绝时更新同一 audit 行为脱敏终态
3. 校验 UUIDv4 idempotency_key；服务端生成 document_id/URI/src 与固定安全标签
4. 渲染完整候选 Markdown，再扫描最终字节并计算 content_sha256
5. Vault 同一文件系统写 staging，chmod 0600，fsync 文件
6. SQLite BEGIN IMMEDIATE：插入 prepared 指针事件并把 audit 更新为 prepared，原子提交
7. atomic rename staging → 正式 MD，fsync 父目录
8. SQLite 同一事务将事件置 ready、audit 置 stored_pending，提交
9. 返回 accepted:true / stored:true / indexed:false / index_state:index_pending
```

失败语义：

- prepared 提交前失败：删除 staging，audit 更新为脱敏终态，返回 `accepted:false/stored:false`；
- prepared 已提交但 rename 失败：后台允许恢复，必须明确返回 `accepted:true/stored:false/index_state:recovery_pending`，不能返回普通失败后再静默落盘；
- rename 成功但 ready/audit 终态提交失败：返回 `accepted:true/stored:true/index_state:recovery_pending`；
- 同 client + 同 idempotency key + 同 `content_sha256` 返回已有结果；同 key + 不同 hash 返回 `409 HIPPOCAMPUS_IDEMPOTENCY_CONFLICT`；
- 每次重试请求有新的 request_id/audit 行，但只关联原有 Outbox 事件（经 root_request_id/event_id），不新建第二份正式 MD。

## 5.3 Phase 1 内部 startup recovery（不暴露为 MCP 工具）

- 清理/恢复 orphan staging；
- `prepared + 正式文件 hash 匹配` → 按**当前**策略重扫后标 `ready`；
- `prepared + staging 匹配` → 按当前策略重扫通过后才补 rename/fsync/`ready`；
- `Vault 有文件无 state` → 重扫后补建事件；
- 遗留 audit `started/prepared` 收敛为 `interrupted/recovery_pending`，不新增第二行；
- scanner 不可用 → 停止恢复不补队列；命中秘密 → `policy_blocked`；hash 不符 → `conflict` 不猜测覆盖。

Phase 3 才提供管理员 `memory_reconcile`/`memory_reindex`。所有 retry、dead replay 和 recovery 都必须重新校验 path、symlink、SHA、scan policy 与精确 retain body；不得信任旧扫描结论。dead 只保留状态和枚举错误码。

## 5.4 单实例 index worker 与 Chunking 永久边界

```text
原子 lease ready 事件（lease 有过期回收）
→ 解析 URI，拒绝 symlink/穿越
→ 校验 desired_sha256 与当前文件 SHA
→ 按当前策略重扫文件
→ 只读 frontmatter 的 retrieval_text 与最小 metadata/tags
→ 重扫精确 retain payload
→ POST /v1/default/banks/main/memories
   items[0].update_mode=replace，稳定 document_id
   event_at 为可靠 ISO 时间时携带 timestamp=event_at；event_at=unset 时省略 timestamp 字段（D1）
   async=false
→ 成功且事件仍为最新版本时标记 indexed
```

- 同一 Bank 同时最多一个 retain（规避 [#3227](https://github.com/vectorize-io/hindsight/issues/3227) 死锁）；异步边界在 Hippocampus Outbox，不叠加 Hindsight async operation 队列；
- 指数退避 5s 起、上限 15min、8 次进 `dead`（仅状态，不复制 payload）；新版本到达旧事件标 `superseded`；
- 系统为 at-least-once；最终无重复依赖稳定 `document_id` + `replace` upsert（API/SQL 实测）；
- worker 不回写 Markdown 运行状态；
- **Chunking 永久边界**：向量生成由 Hindsight 对 retrieval_text 内部完成；Hindsight 对 retrieval_text 的内部拆分允许，但 Detail/完整 Markdown 永久不进入 Hindsight/LLM。超长主题在 commit 前拆成多个记忆对象。Phase 3 只能优化或拆分已脱敏 retrieval_text；正文 chunking 不属于当前路线图，未来如需评估必须作为独立架构变更再次取得用户明确授权，秘密红线永不放宽。

## 5.5 `memory_status` 状态映射

内部状态：`prepared / ready / indexing / retry_wait / recovery_pending / indexed / dead / superseded / policy_blocked / conflict`。

外部 `index_state` 映射固定：

- `ready/retry_wait → index_pending`；
- `prepared/recovery_pending → recovery_pending`；
- `indexing/indexed/dead/policy_blocked/conflict` 保留同名；
- `superseded` 只在请求当前 client 有权看到对应新版本时返回。

可返回：document ID、状态、attempt、next retry 时间、安全错误码。不返回正文、索引文本、异常原文、Header、Token、其他客户端不可见 ID 或死信内容。

---

*署名：Claude*

# 05 · Outbox 与一致性定稿（v4.1）

> 属于 [Hippocampus 实施计划 v4.1](00-overview-v4.md) · 修订：Codex · 2026-08-08

## 5.1 SQLite 边界

- 文件 `/home/kkp/hippocampus/state/outbox.db`（registry/reservation/outbox/audit 同库分表）；state 目录 `0700`，主库/WAL/SHM `0600`（`umask 077`）；`journal_mode=WAL`、`synchronous=FULL`、`foreign_keys=ON`；仅本地文件系统，不放 NFS/SMB；
- reservation/Outbox 不保存 title、summary、retrieval_text、正文、Header、LLM 请求/响应或异常原文；
- `idempotency_reservation`：`client_id, idempotency_key, payload_hmac, canonical_version, event_id, root_request_id, server_bank_id, document_id, uri, document_created_at, state, lease_owner, lease_expires_at, created_at, updated_at`；主键 `(client_id,idempotency_key)`，`event_id/document_id/uri/root_request_id` 唯一；`server_bank_id` 只能由运行模式生成，客户端不可传；
- `outbox`：`event_id PK/FK, root_request_id, client_id, idempotency_key, server_bank_id, document_id, uri, event_type, desired_sha256, previous_sha256, scan_policy_version, status, attempt, next_attempt_at, lease_owner, lease_expires_at, error_code, created_at, updated_at`；唯一约束 `(document_id,desired_sha256,event_type)`；`event_id` 外键指向 reservation，删除 audit 行不得级联到两表；
- 三张状态域分别用 SQLite `CHECK` 固定：reservation=`reserved|retryable_failed|prepared|stored|rejected|conflict`；Outbox=`prepared|ready|indexing|retry_wait|recovery_pending|indexed|dead|superseded|policy_blocked|conflict`；audit 见 [07 §7.6](07-mcp-tools-audit.md)。合法主路径为 reservation `reserved→prepared→stored`、Outbox `prepared→ready→indexing→indexed`；retry/阻断只能走明列状态，不允许自由字符串；
- `payload_hmac` 是对规范化、已授权且已扫描的客户端控制字段计算的 HMAC-SHA-256，只存 SQLite；与完整文件完整性使用的 `content_sha256/desired_sha256` 分工明确。秘密拒绝请求不得计算或保存 fingerprint；
- 普通 Agent 的 `idempotency_key` 只允许 UUIDv4；`document_id/uri/request_id` 全由服务端生成，客户端提交这些保留字段直接拒绝；
- `error_code` 仅脱敏枚举（`HINDSIGHT_TIMEOUT`、`UPSTREAM_5XX`、`HASH_MISMATCH` 等），不存 traceback 或响应正文。

## 5.2 主写链路（可恢复状态机）

```text
1. 认证后插入最小 audit(request_id UNIQUE,state=started)；失败立即拒绝
2. 按 04/07 完成 JSON-RPC ingress、schema、授权和全部业务字段秘密扫描
3. 版本化规范化客户端 payload，计算 payload_hmac
4. SQLite BEGIN IMMEDIATE：先查 (client_id,idempotency_key)
   - 已存在且 HMAC 相同：本次 audit 关联既有 root_request_id/event_id；返回既有终态/当前状态，或取得过期 reservation lease 后恢复同一事件
   - 已存在但 HMAC 不同：audit 记安全冲突并返回 409，不生成文件/事件
   - 不存在：才生成 event/document ID、URI、文档时间和当前运行模式固定的 server_bank_id；root_request_id=本次 request_id；插入 reserved reservation 并关联 audit
5. 用 reservation 中固定的 ID/URI/时间渲染完整 Markdown；扫描最终字节并计算 content_sha256
6. 在目标目录按 event_id 推导唯一相对名 `.hippocampus-<event_id>.staging`，以 O_EXCL/O_NOFOLLOW、0600 安全独占创建；写完后 fsync(file) 再 fsync(parent dir)
7. SQLite BEGIN IMMEDIATE：插入 prepared Outbox 指针，reservation/audit 更新为 prepared，原子提交
8. atomic rename staging → 正式 MD，再次 fsync(parent dir)
9. SQLite 同一事务将 Outbox 置 ready、reservation 置 stored、audit 置 stored_pending，提交
10. 将同一 audit 行终态更新为 ok，安全 outcome_code=COMMIT_STORED_INDEX_PENDING；提交成功后才返回 accepted:true / stored:true / indexed:false / index_state:index_pending；更新失败按下述真实持久化状态返回 recovery_pending
```

payload 规范化规则必须版本化：UTF-8/NFC、键排序、无额外空白、可选字段采用固定 absent/default 表示；只覆盖客户端控制的已授权字段；排除 Authorization、JSON-RPC id、request/event/document ID、URI、服务端标签和服务端时间。禁止重新生成服务端字段后用 `content_sha256` 判断业务重试。

失败语义：

- reservation 后、prepared 前失败：删除 staging，reservation/audit 更新为脱敏 `retryable_failed` 或终态 `rejected`；同 payload 重试复用原 ID/URI/时间；
- prepared 已提交但 rename 失败：后台允许恢复，必须明确返回 `accepted:true/stored:false/index_state:recovery_pending`，不能返回普通失败后再静默落盘；
- rename 成功但 ready/audit 终态提交失败：返回 `accepted:true/stored:true/index_state:recovery_pending`；
- prepared 状态下 staging 与正式文件都缺失：标 `conflict` + `DURABILITY_GAP`，停止自动猜测或重建；本地 `/readyz=503`、安全计数器、registry/status CLI 与验收报告必须显示该码，禁止因此创建 Gotify/webhook；
- 同 client + 同 key + 同 `payload_hmac` 返回已有结果；同 key + 不同 HMAC 返回 `409 HIPPOCAMPUS_IDEMPOTENCY_CONFLICT`；`content_sha256` 只负责文件完整性；
- 每次重试有新的 request_id/audit 行，并通过不可变 `root_request_id+event_id` 关联同一逻辑事件；Outbox 根请求不得被覆盖。并发相同 key 只有 lease owner 执行文件状态机，其他请求返回同一事件当前状态；worker 只用 `event_id+attempt` 跟踪。

## 5.3 Phase 1 内部 startup recovery（不暴露为 MCP 工具）

- 只恢复能由 reservation 的 event_id/URI 推导且 owner、regular-file、0600、SHA 均匹配的 staging；无 state 的 staging 一律 `unowned_orphan`，不自动删除或纳管；
- 过期 `reserved` lease 可释放；reservation 不保存 payload，服务端不得自行重建，必须等待同 client 用同 key+payload 重试并复用既有 ID/URI/时间；
- `prepared + 正式文件 hash 匹配` → 按**当前**策略重扫后标 `ready`；
- `prepared + staging 匹配` → 按当前策略重扫通过后才补 rename/fsync/`ready`；
- `Vault 有文件无 reservation/Outbox provenance` → 只计为 `unowned_orphan`，禁止 read/index/补事件，也不信任其 frontmatter；等待 Phase 3 获得授权的 reconcile/import；
- startup 使用 `process_instance_id`/过期 lease 收敛所有非终态 audit：关联 event 的 commit origin 按真实文件/state 更新同一行；非 commit、lifecycle 与 replay 无法证明已安全返回时更新为 `interrupted/AUDIT_FINALIZE_LOST`；不新增替代行，后续客户端重放保留自己的 audit 行；
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
→ POST /v1/default/banks/<reservation.server_bank_id>/memories
   items[0].update_mode=replace，稳定 document_id
   metadata.event_at 始终为 RFC3339|unset 字符串
   event_at 可靠时 timestamp=规范化 RFC3339；event_at=unset 时 timestamp="unset"
   async=false
→ 成功且事件仍为最新版本时标记 indexed
```

- `server_bank_id` 仅允许正式 `main` 或本版隔离 commissioning Bank，来自 reservation 而非客户端；同一 Bank 同时最多一个 retain（规避 [#3227](https://github.com/vectorize-io/hindsight/issues/3227) 死锁）；异步边界在 Hippocampus Outbox，不叠加 Hindsight async operation 队列；
- 指数退避 5s 起、上限 15min、8 次进 `dead`（仅状态，不复制 payload）；新版本到达旧事件标 `superseded`；
- 系统为 at-least-once；最终无重复依赖稳定 `document_id` + `replace` upsert（API/SQL 实测）；
- worker 不回写 Markdown 运行状态；
- **Chunking 永久边界**：向量生成由 Hindsight 对 retrieval_text 内部完成；Hindsight 对 retrieval_text 的内部拆分允许，但 Detail/完整 Markdown 永久不进入 Hindsight/LLM。超长主题在 commit 前拆成多个记忆对象。Phase 3 只能优化或拆分已脱敏 retrieval_text；正文 chunking 不属于当前路线图，未来如需评估必须作为独立架构变更再次取得用户明确授权，秘密红线永不放宽。

## 5.5 `memory_status` 状态映射

状态投影先看 Outbox；存在 Outbox 时其状态为权威。无 Outbox 时才读取 reservation：`reserved/retryable_failed→recovery_pending`，`rejected→rejected`，`conflict→conflict`；`reservation=stored` 却无 Outbox、或 reservation/Outbox 组合不满足上述合法转换，统一变成 `conflict/STATE_INVARIANT_BROKEN` 并令 `/readyz=503`。

外部 `index_state` 映射固定：

- `ready/retry_wait → index_pending`；
- `prepared/recovery_pending → recovery_pending`；
- `reserved/retryable_failed → recovery_pending`；
- `indexing/indexed/dead/policy_blocked/conflict/rejected` 保留同名；
- `superseded` 只在请求当前 client 有权看到对应新版本时返回。

可返回：document ID、状态、attempt、next retry 时间、安全错误码。不返回正文、索引文本、异常原文、Header、Token、其他客户端不可见 ID 或死信内容。

---

*修订：Codex*

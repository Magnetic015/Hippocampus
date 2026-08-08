# 05 · Outbox 与一致性定稿（v4.2）

> 属于 [Hippocampus 实施计划 v4.2](00-overview-v4.md) · final · 2026-08-08

## 5.1 SQLite 边界

- 文件 `/home/kkp/hippocampus/state/outbox.db`（registry/reservation/outbox/audit 同库分表）；state 目录 `0700`，主库/WAL/SHM `0600`（`umask 077`）；`journal_mode=WAL`、`synchronous=FULL`、`foreign_keys=ON`；仅本地文件系统，不放 NFS/SMB；
- reservation/Outbox 不保存 title、summary、retrieval_text、正文、Header、LLM 请求/响应或异常原文；
- `idempotency_reservation`：`client_id, idempotency_key, payload_hmac, canonical_version, event_id, root_request_id, server_bank_id, document_id, uri, document_created_at, state, lease_owner, lease_expires_at, created_at, updated_at`；主键 `(client_id,idempotency_key)`，`event_id/document_id/uri/root_request_id` 唯一；`server_bank_id` 只能由运行模式生成，客户端不可传；
- `outbox`：`event_id PK/FK, root_request_id, client_id, idempotency_key, server_bank_id, document_id, uri, event_type, desired_sha256, previous_sha256, scan_policy_version, status, attempt, next_attempt_at, lease_owner, lease_expires_at, error_code, created_at, updated_at`；唯一约束 `(document_id,desired_sha256,event_type)`；`event_id` 外键指向 reservation，删除 audit 行不得级联到两表；
- 三张状态域分别用 SQLite `CHECK` 固定：reservation=`reserved|retryable_failed|prepared|stored|rejected|conflict`；Outbox=`prepared|ready|indexing|retry_wait|indexed|dead|policy_blocked|conflict`；audit 见 [07 §7.6](07-mcp-tools-audit.md)。合法主路径为 reservation `reserved→prepared→stored`、Outbox `prepared→ready→indexing→indexed`；重试环固定 `indexing→retry_wait→indexing`（worker 在认领 lease 的同一原子 UPDATE 内置 `indexing`），终止/阻断转换仅 `indexing→dead|policy_blocked|conflict`；不允许自由字符串；
- **lease 契约（F4）**：`lease_owner` 恒为持有进程的 `process_instance_id`（进程启动时生成的 UUIDv4，进程存续期内不变，与 audit 同值）；`lease_expires_at` 以本库 `unixepoch()` 为唯一时钟源（单机部署，不引入外部时钟）。reservation lease 在 [§5.2](#52-主写链路可恢复状态机) 步骤 4 的同一 `BEGIN IMMEDIATE` 内取得或接管，TTL 60 s，owner 可在未过期时续租（仅限自身），事件到达终态或移交 Outbox 后同事务释放。Outbox lease 由 worker 以单条原子 `UPDATE`（仅命中 `ready`/到期 `retry_wait` 且 lease 为空或已过期的行）认领，TTL 300 s，每次 attempt 前续租。过期 lease 只能被同样的原子条件更新（CAS 语义）接管；未持有效 lease 的进程不得执行文件或索引状态机；
- `canonical_version`：payload 规范化规则的版本号，一期固定 `cv1`；规范化规则的任何变更必须以新值发布并作为计划修订。`payload_hmac` 比较只在同 `canonical_version` 内定义：同 key 而 `canonical_version` 不同按 `409 HIPPOCAMPUS_IDEMPOTENCY_CONFLICT` 处理（客户端应换新 key 重新提交）；
- `scan_policy_version`：秘密扫描器「规则集 + 构建」的单调版本号，形如 `sp<N>`，一期初始 `sp1`，内嵌于扫描器组件，规则或构建任何变更必须递增；恢复、retry、dead replay、worker 均以当前值对照存量值，不同即按当前策略重扫（见 §5.3/§5.4）；
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
   - 不存在：才生成 event/document ID、URI、文档时间和当前运行模式固定的 server_bank_id；root_request_id=本次 request_id；插入 reserved reservation（同事务取得 lease）并关联 audit
5. 用 reservation 中固定的 ID/URI/时间渲染完整 Markdown；扫描最终字节并计算 content_sha256
6. 在目标目录按 event_id 推导唯一相对名 `.hippocampus-<event_id>.staging`，以 O_EXCL/O_NOFOLLOW、0600 安全独占创建；写完后 fsync(file) 再 fsync(parent dir)
7. SQLite BEGIN IMMEDIATE：插入 prepared Outbox 指针，reservation/audit 更新为 prepared，原子提交
8. atomic rename staging → 正式 MD，再次 fsync(parent dir)
9. SQLite 同一事务将 Outbox 置 ready、reservation 置 stored（释放 reservation lease）、audit 置 stored_pending，提交
10. 将同一 audit 行终态更新为 ok，安全 outcome_code=COMMIT_STORED_INDEX_PENDING；提交成功后才返回 accepted:true / stored:true / indexed:false / index_state:index_pending；更新失败按下述真实持久化状态返回 recovery_pending
```

payload 规范化规则必须版本化（`canonical_version`，一期 `cv1`）：UTF-8/NFC、键排序、无额外空白、可选字段采用固定 absent/default 表示；只覆盖客户端控制的已授权字段；排除 Authorization、JSON-RPC id、request/event/document ID、URI、服务端标签和服务端时间。禁止重新生成服务端字段后用 `content_sha256` 判断业务重试。

失败语义：

- reservation 后、prepared 前失败：删除 staging，reservation/audit 更新为脱敏 `retryable_failed` 或终态 `rejected`；同 payload 重试复用原 ID/URI/时间；
- prepared 已提交但 rename 失败：后台允许恢复，必须明确返回 `accepted:true/stored:false/index_state:recovery_pending`，不能返回普通失败后再静默落盘；
- rename 成功但 ready/audit 终态提交失败：返回 `accepted:true/stored:true/index_state:recovery_pending`；
- prepared 状态下 staging 与正式文件都缺失：同一事务将 reservation 与 Outbox 行**均**置 `conflict`，`error_code=DURABILITY_GAP`，停止自动猜测或重建；本地 `/readyz=503`、安全计数器、registry/status CLI 与验收报告必须显示该码，禁止因此创建 Gotify/webhook；
- 同 client + 同 key + 同 `payload_hmac`（同 `canonical_version`）返回已有结果；同 key + 不同 HMAC 返回 `409 HIPPOCAMPUS_IDEMPOTENCY_CONFLICT`；`content_sha256` 只负责文件完整性；
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
原子 lease ready/到期 retry_wait 事件（同一 UPDATE 置 indexing，lease 有过期回收）
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
→ 成功时标记 indexed
```

- `server_bank_id` 仅允许正式 `main` 或本版隔离 Bank `commissioning-v4-2`，来自 reservation 而非客户端；同一 Bank 同时最多一个 retain（规避 [#3227](https://github.com/vectorize-io/hindsight/issues/3227) 死锁）；异步边界在 Hippocampus Outbox，不叠加 Hindsight async operation 队列；
- 指数退避 5s 起、上限 15min、8 次进 `dead`（仅状态，不复制 payload）；一期无更新流，同一 document 仅有单一事件，supersede 类状态域扩展属 Phase 3 计划修订；
- 系统为 at-least-once；最终无重复依赖稳定 `document_id` + `replace` upsert（API/SQL 实测）；lease 过期后的重复投递不产生重复文档或状态回退；
- worker 不回写 Markdown 运行状态；
- **Chunking 永久边界**：向量生成由 Hindsight 对 retrieval_text 内部完成；Hindsight 对 retrieval_text 的内部拆分允许，但 Detail/完整 Markdown 永久不进入 Hindsight/LLM。超长主题在 commit 前拆成多个记忆对象。Phase 3 只能优化或拆分已脱敏 retrieval_text；正文 chunking 不属于当前路线图，未来如需评估必须作为独立架构变更再次取得用户明确授权，秘密红线永不放宽。

## 5.5 `memory_status` 状态映射

状态投影先看 Outbox；存在 Outbox 时其状态为权威。无 Outbox 时才读取 reservation：`reserved/retryable_failed→recovery_pending`，`rejected→rejected`，`conflict→conflict`；`reservation=stored` 却无 Outbox、或 reservation/Outbox 组合不满足上述合法转换，统一变成 `conflict/STATE_INVARIANT_BROKEN` 并令 `/readyz=503`。

外部 `index_state` 映射固定（`recovery_pending` 只是对外投影值与 audit 状态，不是 Outbox 列值）：

- Outbox `ready/retry_wait → index_pending`；
- Outbox `prepared → recovery_pending`；
- reservation `reserved/retryable_failed → recovery_pending`（无 Outbox 时）；
- Outbox `indexing/indexed/dead/policy_blocked/conflict` 与 reservation `rejected` 保留同名。

可返回：document ID、状态、attempt、next retry 时间、安全错误码。不返回正文、索引文本、异常原文、Header、Token、其他客户端不可见 ID 或死信内容。

---

*v4.2 final*

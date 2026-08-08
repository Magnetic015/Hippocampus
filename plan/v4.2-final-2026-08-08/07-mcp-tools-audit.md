# 07 · Hippocampus MCP 一期工具契约与操作审计（v4.2）

> 属于 [Hippocampus 实施计划 v4.2](00-overview-v4.md) · final · 2026-08-08

所有工具先执行 [04 §4.1/§4.5](04-security.md) 的同一认证、授权、schema、秘密扫描和 audit gate。客户端参数只能缩小服务端权限，不能扩大；Hindsight 过滤结果还必须经 Hippocampus 后置鉴权和输出扫描。

## 7.0 Streamable HTTP 与 JSON-RPC ingress gate

- `/mcp/` 的 POST/GET/DELETE 均按 [04 §4.1](04-security.md) 固定顺序处理：**source 并集 gate → 认证 → 才解析 body 或查询 session**；客户端自报 request-id Header 永远不作为服务端 ID；
- 一期每个 HTTP request 只接受一个 JSON-RPC object，拒绝 batch array、`Content-Encoding`、未知 Content-Type、尾随数据和重复 JSON key；POST 只接受 `application/json`；
- 硬上限：HTTP body 256 KiB、JSON depth 8、单 object 64 fields、单 array 64 items、envelope method/name/id 各 256 UTF-8 bytes；超限整笔拒绝，不做部分处理；
- 服务端 request_id 与 JSON-RPC id 完全分离。原始 method/id/tool name/未知字段名先在无日志 parser 中有界解析并扫描，绝不持久化；通过后只映射固定 `operation_enum` 与四个 `tool_enum`；未知工具为 `invalid_tool`，未知键为 `unknown_field`；
- 安全 JSON-RPC id 只为协议响应瞬时回显；非法、超限或命中 canary/秘密时响应使用 `id:null`，错误只含固定 code/message，不含 parser 原文、原始键名、URI、body 或 traceback；
- `initialize`、`notifications/initialized`、`tools/list`、session GET/DELETE 也要求认证；一期拒绝 batch 的兼容性必须由三个真实客户端在 Phase 0/2 证明，任一客户端必须依赖 batch 即 No-Go；
- 关闭框架 request/response body、自动异常详情和原始 JSON 日志。一个已认证 HTTP request 对应一条最小 audit；batch 被整体拒绝，不拆成多条伪调用。

## 7.1 `memory_search`

- 输入：query、单个授权 project 与可选 type/source 过滤；query 在 audit HMAC 和 Hindsight 调用前先扫描，扫描器不可用即拒绝；
- query 最多 4096 个 Unicode scalar；Hindsight recall 固定 `budget=low`、`max_tokens=2048` 为一期默认，token budget 不等于结果条数；
- 每次请求至少追加 `project:<project>` 和 `scope:shared` 的 `tag_groups + all_strict` 组；`src:*` 仅在调用者显式请求且值通过 allowlist 时追加，**不得默认限制为当前客户端来源**，以保持跨 Agent 共享；
- Hindsight 返回后，逐条拒绝缺失/多重 project 标签、URI/project 不一致、越权或保留字段异常的结果，再做最终秘密扫描；随后按 `document_id` 分组，同组 URI/metadata/tags 冲突则整组丢弃，正常组取最高 final score 的安全 snippet；最后按组最高分排序并裁剪最多 5 个**唯一文档**卡片；
- 只返回 `document_id, uri, index_title, index_summary, safe_snippet, tags, source_agent, trust, event_at, index_state`；不返回 Detail、完整 retrieval_text、主机路径或 Hindsight 内部 ID；
- 卡片 `event_at` 只取经后置校验的 metadata 字符串；Hindsight 推断的 `occurred_start/occurred_end/mentioned_at` 不得覆盖或冒充；
- Hindsight 不可用返回 `HIPPOCAMPUS_INDEX_UNAVAILABLE`，不做全文遍历式隐式降级。

## 7.2 `memory_read`

- 输入授权范围内 `memory://` URI；单次默认最多 1–2 篇；从 URI 解析 project 后必须再次对 registry 授权；
- 不调用 Hindsight `get_document`；由 Hippocampus 按 [03 §3.2](03-data-model.md) 唯一映射读取 Vault；
- 读取前验证路径、symlink、SHA 与当前扫描策略版本；响应也做最终安全扫描；`policy_blocked/conflict` 不返回。
- 单 URI 最多返回 192 KiB，单调用总响应最多 256 KiB；超限返回安全错误并建议把主题拆成多个记忆对象，不做静默截断。

## 7.3 `memory_commit`

必填：client-scoped UUIDv4 `idempotency_key`、title、summary、retrieval_text、project 与 type 请求值。可选：`detail_body`、`event_at`；无可靠事件时间时服务端写 `unset`，retain 顶层 timestamp 与 metadata event_at 都显式发送 `"unset"`（见 [03 §3.4](03-data-model.md)）。缺少 Detail 时仍生成带空 Detail 节的完整 MD。

字段上限：title 256 Unicode scalar、summary 60–200 Unicode scalar、retrieval_text 100–600 tokens（硬上限 800；按 [03 §3.3](03-data-model.md) 固定的 tiktoken `cl100k_base` 实现计数）、detail_body 128 KiB、event_at/project/type 按固定 schema；渲染后完整 Markdown 不超过 192 KiB，整个 JSON-RPC body 仍受 §7.0 的 256 KiB 上限。超限在 reservation/文件写入前拒绝。

服务端生成：request/document ID、URI、`source_agent/src:*`、`scope:shared`、`sensitivity:internal`、`trust:agent`、server Bank、文档时间与运行状态。客户端自报 Bank 或其他保留字段不是"覆盖"，而是整笔拒绝（`RESERVED_FIELD_FORBIDDEN`）。project/type 必须过 registry allowlist；commissioning 模式只允许 project `commissioning` 并固定到本版隔离 Bank，正式模式固定到 `main`。

正常响应：

```json
{
  "accepted": true,
  "stored": true,
  "indexed": false,
  "index_state": "index_pending",
  "document_id": "mem_20260808_01HXYZ",
  "uri": "memory://shared/piworkspace/mem_20260808_01HXYZ"
}
```

已知限制：一期无 `memory_update`，同一知识用不同 idempotency key 提交会产生并存条目。commit 前 search 只是客户端最佳努力提示，不是服务端去重保证；只有**同一 client、同一 UUIDv4 key、同一 payload** 的重试才幂等。update/合并/淘汰归 Phase 3。

## 7.4 `memory_status`

按服务端 document ID 或 UUIDv4 idempotency key 查询；先按当前 client/project 授权，只返回 [05 §5.5](05-outbox-worker.md) 安全状态、attempt、next retry 与安全错误码；不泄露 payload、异常正文或其他客户端不可见标识。

## 7.5 一期不向普通 Agent 暴露

Hindsight 原生 retain/recall/reflect/get_document；delete bank / clear memories / delete document；任意文件读写；Bank 配置修改；`memory_update` / `memory_reindex` / `memory_reconcile` / `memory_forget`；registry 管理 CLI；Control Plane（一期进程本身关闭）。

## 7.6 操作审计日志（一期交付）

- 存储：SQLite `audit` 表（与 outbox 同库不同表），`request_id UNIQUE`；`CHECK(state IN started,prepared,stored_pending,ok,idempotent_replay,rejected,error,interrupted,recovery_pending)`；普通主路径 `started→ok`，commit 可走 `started→prepared→stored_pending→ok`，其余只走列出的终态。`state=recovery_pending` 的唯一写入时机：commit 已 prepared 但 rename 未完成、服务端如实返回 `index_state:recovery_pending` 时，该请求的终态更新写入 `recovery_pending`；startup recovery 完成后由 reconciler 按真实文件/state 把该行收敛为 `ok` 或 `error`（`stored_pending` 卡滞行同理收敛）；
- **已认证请求**：每个已认证 MCP HTTP request 恰有一行。初始只写 `ts, request_id, process_instance_id, client_id, state=started`，`operation_enum/tool_enum/root_request_id/event_id` 均为空；不得含任何客户端字段。envelope 通过后才写 allowlisted `operation_enum`，tools/call 只写四个 canonical `tool_enum` 或 `invalid_tool`；
- **未认证拒绝行（F1）**：并集 gate 拒绝与认证失败的请求也各恰有一行，由服务端一次性写入终态：`request_id, ts, process_instance_id, client_id=NULL, source_ip=socket peer, state=rejected, outcome_code∈{source_denied, auth_failed}`，operation/tool/root/event 均空；不记录所试 Token、Header 或 `X-Forwarded-For`。`source_ip` 列仅这两类行非空，已认证行恒为 NULL（peer 已由并集 gate 与 client→peer 绑定证明）；SQLite 不可写时这两类拒绝不产生行，请求直接 fail-closed，由 `/readyz` 与本地安全计数器暴露（与 [04 §4.1](04-security.md) 同口径）；
- 完整安全字段：`ts, request_id, process_instance_id, root_request_id|null, event_id|null, client_id|null（仅未认证拒绝行为空）, source_ip|null（仅未认证拒绝行非空）, operation_enum|null, tool_enum|null, normalized_document_id|uri|null, state, outcome_code, latency_ms, scan_policy_version`。首次 commit 的 `root_request_id=request_id`；后续每次重放保留自己的 request_id 并指向既有 root/event；非 commit 两列为空；在保留期内形成 `1 event : N audit requests`；
- 不记录正文、title/summary/retrieval_text、Header、Token、JSON-RPC id/method/name、原始 URI/键名或异常原文。安全 query 只记录使用独立 audit key 的 `HMAC-SHA-256 + 长度`；秘密 query 不计算/保存 HMAC 或长度，只记规则类别与 schema 枚举路径；
- 不提供任何"截断 query 明文"开关；秘密拒绝按 [04 §4.2](04-security.md) 只记录脱敏决策；
- audit insert 不可写时不得继续 envelope/业务 schema/扫描、文件读写或 Hindsight 调用；`/healthz`、`/readyz` 仍可报告故障；
- 普通成功响应必须在脱敏终态 audit update 提交后：search/read/status 终态更新失败时丢弃结果并返回 `HIPPOCAMPUS_AUDIT_UNAVAILABLE`；commit 若已持久化则返回真实 accepted/stored 状态和 `recovery_pending`，由 startup recovery 更新 origin 行，绝不伪报未发生；拒绝路径 update 失败返回通用 audit unavailable，不回显原始输入；
- 默认保留 90 天并受行数上限约束。JSONL 仅导出到 `/home/kkp/hippocampus/state/audit-export/`（目录 `0700`、文件 `0600`），使用临时文件 + fsync + rename；不进 Vault、Git、一期备份枢纽或远程端点；
- `request_id` 标识一次入站请求；`root_request_id+event_id` 贯穿 reservation、Outbox 和 worker。audit.event_id 可作为指向 reservation 的非级联外键；reservation/outbox 绝不反向依赖 audit，`root_request_id` 是耐久相关值而非指向 90 天 audit 行的 FK。清理旧 audit 不级联、不阻塞、不破坏 event；保留期内用 index/SQL join 验证 `1:N`，跨 90 天只保证 reservation 中的 root/event 值继续存在；
- startup stale-audit reconciler 依据 `process_instance_id` 与过期 lease 更新原行：commit 按真实 event/state 收敛；非 commit、lifecycle 和 replay 无法证明已返回时收敛为 `interrupted/AUDIT_FINALIZE_LOST`，不得永久遗留 `started` 或新建替代行。

---

*v4.2 final*

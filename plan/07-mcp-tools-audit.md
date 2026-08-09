# 07 · Hippocampus MCP 一期工具契约与操作审计（v4）

> 属于 [Hippocampus 实施计划 v4](00-overview-v4.md) · 署名：Claude · 2026-08-08

所有工具先执行 [04 §4.1/§4.5](04-security.md) 的同一认证、授权、schema、秘密扫描和 audit gate。客户端参数只能缩小服务端权限，不能扩大；Hindsight 过滤结果还必须经 Hippocampus 后置鉴权和输出扫描。

## 7.1 `memory_search`

- 输入：query、单个授权 project 与可选 type/source 过滤；query 在 audit HMAC 和 Hindsight 调用前先扫描，扫描器不可用即拒绝；
- Hindsight recall 固定 `budget=low`、`max_tokens=2048` 为一期默认；MCP 再独立裁剪最多 5 张卡，token budget 不等于结果条数；
- 每次请求至少追加 `project:<project>` 和 `scope:shared` 的 `tag_groups + all_strict` 组；`src:*` 仅在调用者显式请求且值通过 allowlist 时追加，**不得默认限制为当前客户端来源**，以保持跨 Agent 共享；
- Hindsight 返回后，服务端拒绝缺失/多重 project 标签、URI/project 不一致、越权或保留字段异常的结果，再做最终秘密扫描；
- 只返回 `document_id, uri, index_title, index_summary, safe_snippet, tags, source_agent, trust, event_at, index_state`；不返回 Detail、完整 retrieval_text、主机路径或 Hindsight 内部 ID；
- 卡片 `event_at` 只取经后置校验的 metadata 字符串；Hindsight 推断的 `occurred_start/occurred_end/mentioned_at` 不得覆盖或冒充；
- Hindsight 不可用返回 `HIPPOCAMPUS_INDEX_UNAVAILABLE`，不做全文遍历式隐式降级。

## 7.2 `memory_read`

- 输入授权范围内 `memory://` URI；单次默认最多 1–2 篇；从 URI 解析 project 后必须再次对 registry 授权；
- 不调用 Hindsight `get_document`；由 Hippocampus 映射并读取 Vault；
- 读取前验证路径、symlink、SHA 与当前扫描策略版本；响应也做最终安全扫描；`policy_blocked/conflict` 不返回。

## 7.3 `memory_commit`

必填：client-scoped UUIDv4 `idempotency_key`、title、summary、retrieval_text、project 与 type 请求值。可选：`detail_body`、`event_at`；无可靠事件时间时服务端写 `unset`（此时 retain 载荷省略 timestamp 字段，见 [03 §3.4](03-data-model.md)）。缺少 Detail 时仍生成带空 Detail 节的完整 MD。

服务端生成：request/document ID、URI、`source_agent/src:*`、`scope:shared`、`sensitivity:internal`、`trust:agent`、文档时间与运行状态。客户端自报保留字段不是"覆盖"，而是整笔拒绝（`RESERVED_FIELD_FORBIDDEN`）。project/type 必须过 registry allowlist。

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

Hindsight 原生 retain/recall/reflect/get_document；delete bank / clear memories / delete document；任意文件读写；Bank 配置修改；`memory_update` / `memory_reindex` / `memory_reconcile` / `memory_forget`；Control Plane。

## 7.6 操作审计日志（一期交付）

- 存储：SQLite `audit` 表（与 outbox 同库不同表），`request_id UNIQUE`；状态为 `started/prepared/stored_pending/ok/rejected/error/interrupted/recovery_pending`；
- 每个通过 audit gate 的 MCP 调用恰有一行：`ts, request_id, client_id, tool, document_id|uri, state, outcome_code, latency_ms, scan_policy_version`；完成时更新同一行，不追加第二行；commit 重试的新 request_id 经 root_request_id/event_id 关联同一 Outbox 事件；
- 认证失败生成服务端 request_id，只记录 socket peer source IP 与 `auth_failed`，不记录所试 Token、Header 或 X-Forwarded-For；
- 不记录正文、title/summary/retrieval_text、Header、Token、异常原文。安全 query 只记录使用独立 audit key 的 `HMAC-SHA-256 + 长度`；秘密 query 不计算/保存 HMAC 或长度，只记规则类别与字段路径；
- 不提供任何"截断 query 明文"开关；秘密拒绝按 [04 §4.2](04-security.md) 只记录脱敏决策；
- audit insert 不可写时四个数据工具全部 fail-closed，且不执行文件读取、写入或 Hindsight 调用；`/healthz`、`/readyz` 仍可报告故障；
- 默认保留 90 天并受行数上限约束。JSONL 仅导出到 `/home/kkp/hippocampus/state/audit-export/`（目录 `0700`、文件 `0600`），使用临时文件 + fsync + rename；不进 Vault、Git、一期备份枢纽或远程端点；
- `request_id` 从 Phase 1 起贯穿 audit、Outbox 和 worker retry；startup recovery 收敛遗留状态，保证崩溃后不产生重复 audit 行。

---

*署名：Claude*

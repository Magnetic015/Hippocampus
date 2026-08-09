# 09 · 一期验收、故障注入与 Go / No-Go（v4）

> 属于 [Hippocampus 实施计划 v4](00-overview-v4.md) · 署名：Claude · 2026-08-08

## 10.1 数据分层

- [ ] `memory_search` 只返回卡片，不返回正文。
- [ ] `memory_read` 只从 Vault 返回授权正文。
- [ ] 抓取 Hippocampus→Hindsight 请求：endpoint、`items[]`、`async=false` 正确；只含 retrieval_text、服务端生成的索引 metadata/tags、`retrieval_sha256` 与（仅当 `event_at` 可靠时的）`timestamp`，无 detail_body、完整 Markdown、主机路径或 full `content_sha256`。
- [ ] `event_at: unset` 的记忆其 retain 载荷**不含 timestamp 字段**（D1），Hindsight 接受且不锚定到导入时间。
- [ ] `documents.original_text` 为 NULL，chunk 正文为空。
- [ ] `main` 的 `store_document_text=false`、`audit_log_enabled=false` 无法被普通 Agent 修改。
- [ ] retain 成功、4xx、timeout 和 failover 后，`llm_requests` 与 Hindsight audit 中无请求/响应副本，telemetry/OTEL exporter 关闭（D3），Docker/cliproxy 日志无 Detail 或认证秘密。
- [ ] 清空 disposable Bank 后可仅凭 frontmatter retrieval_text 重建，不读取 Detail。

## 10.2 Outbox 与幂等

failpoint 分别 kill：audit started 插入前后；staging fsync 前后；prepared+audit 提交前后；rename 前后；ready+audit 终态前后；Hindsight retain 成功前后；SQLite indexed ack 前后。恢复后：

- [ ] 无丢失、无双正式 MD；
- [ ] Hindsight 最终无重复 document/memory；
- [ ] 旧事件不误标新版本 indexed；
- [ ] 同 client+同 UUIDv4 key+同 payload 重试复用原事件；同 key+不同 payload 为 409；
- [ ] 同 payload 间隔数秒重试、服务进程重启后重试、首次成功响应丢失后重试，均复用原 ID/URI/服务端时间和 event；每次 audit request_id 独立且经 root_request_id/event_id 关联；
- [ ] `prepared+staging` 在策略升级、scanner 不可用、人工注入 canary 三种情况下都不会未经当前策略扫描就 rename/index；
- [ ] orphan 安全文档可补队列；含 canary 的 orphan 进 `policy_blocked`；dead replay 也重新校验全链路；
- [ ] 每个 request_id 的 audit 恰一行，所有 commit 重放可经 event_id 关联同一 Outbox，遗留 started/prepared 收敛为 interrupted/recovery_pending；
- [ ] `memory_status` 正确且无敏感错误正文。

延迟门槛：Hindsight 停止时，100 次 ≤10 KiB 顺序安全提交，`memory_commit` p95 ≤ 500 ms 且全部正确落 pending/recovery；实测不满足则记录基线重新批准阈值，不得删除异步解耦验收。

## 10.3 秘密红线（全部用合成 canary）

- [ ] canary 放入所有记忆字段、`idempotency_key/project/type/filter/status key` 及伪装 Authorization/Header、DSN、PEM、Base64、URL 编码、分片输入；全部在持久化前被 schema 或扫描拒绝，合法 Authorization 仅瞬时认证不落盘；
- [ ] 拒绝后 Vault、staging、PostgreSQL、Hindsight、SQLite 主库/WAL/SHM、Outbox、审计、死信、Docker 日志、trace、cliproxy 日志、`backups-test` 均无 canary；
- [ ] 合法 Authorization 正常调用；把同一 Token 复制到任一业务字段立即拒绝；无效 Token 不进入 access log、hash、异常或审计；
- [ ] secret query 在 audit HMAC 和 Hindsight 调用前拒绝，audit 无 query HMAC/长度；安全 query 只有带独立 key 的 HMAC+长度；
- [ ] 停用扫描器后四个 MCP 数据工具以及 worker、retry、startup recovery 全部 fail-closed；仅健康端点可报告故障；
- [ ] 安全提交后、worker 前人工注入 canary，SHA/二次扫描阻止外发；
- [ ] mock Hindsight 返回含 canary 错误 body，日志/死信/status/审计不记录；
- [ ] 同时抓取 Hippocampus→Hindsight 与 Hindsight→cliproxy 请求：只有允许的安全索引内容，无 Detail、完整 MD、full MD hash 或认证秘密；
- [ ] Hindsight 返回结果经过字段 allowlist、URI/project 一致性和最终秘密扫描，异常结果被丢弃且不回显；
- [ ] 占位符与无实际值的外部秘密引用可通过；
- [ ] 任何标签、客户端、编码、分片均不能绕过；
- [ ] secret/state/audit export 权限、Git 跟踪、shell history、进程 argv、Compose 输出均无实际凭证。

## 10.4 检索

- [ ] 固定 30–50 条测试集与 ground truth；
- [ ] 中文近义 3–5 组、中英混合查询进入 Top 5；
- [ ] 记录实际请求，确认 `budget=low,max_tokens=2048` 已下发，MCP 独立裁剪最多 5 卡；
- [ ] 精确错误串、IP、端口、UUID、主机名可命中；
- [ ] 事件时间故意早于导入/更新时间，更新或重建后时间召回仍锚定 `event_at`；无 `event_at` 的记忆不被错误锚定为导入时间；
- [ ] 卡片 `event_at` 只取经后置校验的 metadata 字符串；Hindsight 推断的 `occurred_start/occurred_end/mentioned_at` 不得覆盖或冒充；
- [ ] project 过滤使用 `tag_groups` strict：不同 project 不串扰，未打标/错标/多 project 条目不混入；
- [ ] `global` project 的通用记忆三客户端均可命中（D6）；
- [ ] A 客户端 commit 后 B/C 在同一授权 project 能进入 Top 5 并 read；`src:A` 保持不变，默认 search 不添加当前客户端 source 过滤；
- [ ] 50 条规模记录 search p50/p95，目标 p95 ≤ 5 s；
- [ ] `simple` lexical 不足只记录为 Phase 3 评估，不在正式 Bank 原地换后端。

## 10.5 故障降级

- [ ] 停 Hindsight/PG 后 MCP 仍启动，commit/read/status 可用；
- [ ] Hindsight 停止时 search 返回明确可重试错误；
- [ ] 恢复后积压按单飞顺序自动完成；
- [ ] SQLite/registry/audit 不可用时四个 MCP 数据工具全部在任何文件/Hindsight 操作前失败；`healthz` 仍可用，`readyz` 为 503；
- [ ] 扫描器不可用时四个 MCP 数据工具全部拒绝，不因旧 SHA、只读/status 参数或 Hindsight 缓存而放行；`healthz` 仍可用，`readyz` 为 503；
- [ ] audit insert/终态更新失败分别通过 failpoint 验证，不出现成功但零审计的调用；
- [ ] `readyz`：Hindsight 故障 200+degraded；扫描器/Vault/SQLite/audit 故障 503；
- [ ] Agent 当前任务不因记忆中枢故障而失败。

## 10.6 客户端与认证

- [ ] 三客户端独立 Token（`HIPPOCAMPUS_CLAUDE_TOKEN`/`HIPPOCAMPUS_CODEX_TOKEN`/`HIPPOCAMPUS_OPENCLAW_TOKEN` 或等价独立引用），变量名、值和 hash 均不同，服务端只存 HMAC；
- [ ] 三客户端 Token 与 `HINDSIGHT_INTERNAL_TOKEN`、`CLIPROXY_KEY`、数据库密码、token pepper、audit key 均不复用；
- [ ] 无 Token 的 MCP initialize/tools/list 为 401；错 Token 401/403；
- [ ] Token 互换、到期、撤销测试通过；单 Token 撤销不影响另两个，`src:*` 仍按实际 Token 映射；
- [ ] Token A 无法 search/read/status 未授权 project；filter 不能扩大权限；
- [ ] 越权 project/type 与 Unicode 混淆/分隔符/路径穿越输入被拒；自报 document_id/URI/tags/trust/scope/sensitivity 返回保留字段错误；
- [ ] 客户端不能直连 Hindsight；
- [ ] Codex 用原生 HTTP + `bearer_token_env_var`；Claude 配置只引用环境变量；OpenClaw 路径已记录、固定、可回滚；
- [ ] OpenClaw 原生能力不足时 Phase 2 确实停止，没有安装 adapter/skill；
- [ ] 应用层只接受记录的精确客户端源地址集合；PoC Token 验收后轮换；长期使用前已另行完成内部 TLS，或用户明确接受 LAN HTTP 残余风险；
- [ ] `.2.41` CLI Gateway 未作为任何配置、路由、模型/MCP endpoint 或测试流量目标；验收报告只允许记录"未参与"的排除结论，不含其配置或运行信息。

## 10.7 审计

- [ ] 每个通过 audit gate 的工具调用（含拒绝/错误）在 audit 表恰一行；commit 的 request_id 可经 root_request_id/event_id 关联 Outbox；重复 request_id 不产生第二行；
- [ ] 认证失败有记录且不含所试 Token；
- [ ] 分别触发非法 UUID、越权 project/type、保留字段、自定义 filter/status、scanner unavailable，并把 canary 放入 JSON-RPC method/id/tool name 与未知字段名；每个已认证调用均恰有一条脱敏 audit 行，只含安全错误码和 schema 枚举路径；未知工具只记 `invalid_tool`、未知键只记 `unknown_field`，且 SQLite 主库/WAL/SHM、日志、Vault/Outbox/Hindsight 均无 canary；
- [ ] 审计不含正文、title/summary/retrieval_text、query 明文、Header、Token、异常原文；安全 query 仅 audit-key HMAC+长度；
- [ ] 秘密拒绝的审计只含规则类别与字段路径；
- [ ] audit started/prepared/终态 failpoint 与进程 crash 后，同一行收敛到正确安全状态；
- [ ] audit sink 不可写时工具 fail-closed；
- [ ] 轮转/保留生效；JSONL 导出同样脱敏，目录/文件为 0700/0600，并发导出不阻塞或破坏 Outbox。

## 10.8 资源与现网边界

- [ ] 连续 60 分钟代表性负载下三容器无 OOM、异常重启或失去健康；
- [ ] 峰值内存不破 3 GiB / 1 GiB / 512 MiB；
- [ ] Pi5 无持续热降频，现有业务无明显抖动；
- [ ] 仅 `192.168.2.41:8888` 为新增监听；DNS、Caddy、防火墙、路由、现有 network 未变；
- [ ] 未新建 Pi5 主机账户，未改 `/etc/passwd`/`/etc/group`；
- [ ] Docker socket owner/group/ACL、`docker` group 成员和远程 daemon 均已只读核对且只有受信管理者；三个容器均未挂载 Docker socket 或等价管理 API；
- [ ] Phase 0 只创建精确命名的本机 workspace/disposable 对象，Pi5 零写入，清理未触及其他 Docker 对象；
- [ ] 未创建 Gotify app/token/webhook、未发送通知；未调用 Backup API、未改 timer、未写/挂载 `/srv/backup`；
- [ ] 未安装 OpenClaw adapter/skill，未探测/调用/读取/修改/重启 Claude CLI Gateway；
- [ ] `backups-test` 只含合成数据的一次性人工导出与临时恢复。

## 11. Go / No-Go

全部满足才允许三客户端写正式 `main`：

1. Hindsight 与 PostgreSQL 镜像已固定 ARM64 digest；
2. retain 主/备、embedding、reranker 与 failover 实测通过；reflect 不作为一期依赖；
3. 正确 `items[] + async=false` retain、`event_at` 时间语义（含省略 timestamp 形态，D1）、replace 重放已验证；
4. `STORE_DOCUMENT_TEXT=false` server+Bank、`AUDIT_LOG=false`、`LLM_TRACE=false`、telemetry/OTEL 关闭（D3）与数据库/exporter/日志结果已验证；
5. Outbox/audit 全部崩溃窗口、幂等重放和 startup recovery 通过；
6. Hindsight 故障不阻断安全写入、已知 URI 读取与状态查询；
7. Hindsight 永远只接收安全索引字段，不接收 Detail、完整 MD 或 full MD hash；
8. 秘密红线全路径、零落盘、无 override 通过；
9. 四工具统一授权、strict+后置过滤、三客户端独立身份/撤销/跨端共享通过；
10. audit gate、HMAC、导出权限与 sink 故障 fail-closed 通过；
11. OpenClaw 原生接入已通过；若需要 adapter，则在获得独立授权并完成其验收前为 No-Go；
12. 长期正式使用已完成内部 TLS，或用户已明确接受受信 LAN HTTP Bearer 残余风险；
13. `.2.41` CLI Gateway 完全未参与；
14. Gotify 通知、备份枢纽、公网和现有业务未被接入或改动。

任一失败即 No-Go。不得以「Agent 直连 Hindsight MCP」「先保存正文再补安全控制」「临时共享 Token」替代。

---

*署名：Claude*

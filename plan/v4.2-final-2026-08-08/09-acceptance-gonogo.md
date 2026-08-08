# 09 · 一期验收、故障注入与 Go / No-Go（v4.2）

> 属于 [Hippocampus 实施计划 v4.2](00-overview-v4.md) · final · 2026-08-08  
> 本文件是**主题式**验收清单与 Go/No-Go 判据；[08](08-phases.md) 每一步的分步验收标准、验证方法与证据见 [11-step-acceptance.md](11-step-acceptance.md)。两者冲突时以更严者为准。

## 10.1 数据分层

- [ ] `memory_search` 只返回卡片，不返回正文。
- [ ] `memory_read` 只从 Vault 返回授权正文。
- [ ] 在 Phase 0 disposable 环境以一次性合成凭证和内存测试探针检查 Hippocampus→mock Hindsight 请求：endpoint、`items[]`、`async=false` 正确；只含 retrieval_text、服务端生成的安全 metadata/tags、`retrieval_sha256`，且顶层 timestamp 与 metadata event_at 同为规范化 RFC3339 或 `"unset"`；无 detail_body、完整 Markdown、主机路径或 full `content_sha256`。探针只输出 allowlisted 字段名与布尔判据，原始 Header/body 不写磁盘或报告；真实凭证环境禁止抓包。
- [ ] `event_at: unset` 的 retain 载荷显式包含 `"timestamp":"unset"` 和 metadata `"event_at":"unset"`；数据库时间字段不锚定导入时间。另证明省略 timestamp 会默认当前时间，因此实现中禁止省略/null。
- [ ] `documents.original_text` 为 NULL，chunk 正文为空。
- [ ] `main` 的 `store_document_text=false`、`audit_log_enabled=false` 无法被普通 Agent 修改。
- [ ] retain 成功、4xx、timeout 和 failover 后，`llm_requests`、Hindsight audit、OTEL exporter/span/event 中无请求/响应副本；`HINDSIGHT_API_OTEL_TRACES_ENABLED=false` 生效，Hindsight 专用 `HINDSIGHT_API_OTEL_EXPORTER_OTLP_ENDPOINT/HEADERS` 与通用 `OTEL_EXPORTER_OTLP_ENDPOINT/HEADERS` 均未设置，Docker/cliproxy 日志无 Detail 或认证秘密。
- [ ] `HINDSIGHT_ENABLE_CP=false` 生效：Hindsight 容器内无 Control Plane 进程/9999 listener，主机无 9999，从 MCP 容器连接 9999 失败。
- [ ] 清空 disposable Bank 后可仅凭 frontmatter retrieval_text 重建，不读取 Detail（一期正式 Bank 无重建工具，边界见 [02 §2.2](02-architecture.md)）。

## 10.2 Outbox 与幂等

failpoint/断电模拟分别覆盖：audit started 前后；reservation insert/lease 前后；staging create/write/fsync(file)/fsync(parent) 前后；prepared+audit 提交前后；rename/fsync(parent) 前后；ready+audit 终态前后；Hindsight retain 成功前后；SQLite indexed ack 前后。failpoint 由测试构建的内置开关注入（仅 disposable/测试镜像包含，正式镜像不含该代码路径）。恢复后：

- [ ] 无丢失、无双正式 MD；
- [ ] 同一 `document_id` 只有一个 current document，replace 后旧 fact set 不残留；同一 retrieval_text 合法提取的多个不同 semantic facts 不按“重复”误判；
- [ ] lease 过期后的重复投递不产生重复文档、重复 retain 结果或状态回退；attempt 单调递增；
- [ ] 同 client+同 UUIDv4 key+同规范化 payload HMAC（同 `canonical_version`）重试复用原事件；同 key+不同 payload 为 409；秘密拒绝不计算/保存 fingerprint；
- [ ] 同 payload 间隔数秒重试、服务进程重启后重试、首次成功响应丢失后重试，均复用原 ID/URI/服务端时间和 event；每次 audit request_id 独立且经 root_request_id/event_id 关联；
- [ ] 多进程并发同 key 同 payload 只有一个 reservation/lease/file/event；并发同 key 不同 payload 只有一方成功，另一方 409；不同 key 相互独立；
- [ ] `prepared+staging` 在策略升级、scanner 不可用、人工注入 canary 三种情况下都不会未经当前策略扫描就 rename/index；
- [ ] 有 reservation provenance 的 staging/正式文件可恢复同一事件；无 state 的 staging/Vault 文件即使扫描安全也只标 `unowned_orphan`，不 read/index/补事件；含 canary 的已拥有对象进 `policy_blocked`；dead replay 重新校验全链路；
- [ ] 每个 request_id 的 audit 恰一行，所有 commit 重放可经 event_id 关联同一 Outbox；origin、非 commit、lifecycle、replay 的遗留非终态行均按 `process_instance_id`/lease 收敛，不永久停在 started/prepared/stored_pending；
- [ ] 状态 `CHECK`、合法转换与投影通过；Outbox 存在时其状态优先，reservation=stored 却无 Outbox 为 `STATE_INVARIANT_BROKEN`；`reserved` 无 payload 时只等待同 payload 重试，不自行重建；
- [ ] 保留期内 SQL join 证明 `1 event : N audit requests`；Outbox root_request_id 不会被重试覆盖，worker attempt 只关联 event_id；模拟 90 天 audit 清理不阻塞、不级联或破坏 reservation/Outbox，root/event 值仍在；
- [ ] prepared 后模拟 staging 与正式文件同时缺失时，reservation 与 Outbox 行均标 `conflict/DURABILITY_GAP` 并 No-Go，不静默新建或猜测成功；`readyz=503`、本地安全计数/CLI 可见，且未创建 Gotify/webhook；
- [ ] `memory_status` 正确且无敏感错误正文。

延迟门槛：Hindsight 停止时，100 次 ≤10 KiB 顺序安全提交，`memory_commit` p95 ≤ 500 ms 且全部正确落 pending/recovery；实测不满足则记录基线重新批准阈值，不得删除异步解耦验收。

## 10.3 秘密红线（全部用合成 canary）

- [ ] canary 放入所有记忆字段、`idempotency_key/project/type/filter/status key` 及伪装 Authorization/Header、DSN、PEM、Base64、URL 编码、分片输入；全部在 reservation/fingerprint/文件前被 schema 或扫描拒绝，合法 Authorization 仅瞬时认证不落盘；
- [ ] 拒绝后 Vault、staging、PostgreSQL、Hindsight、SQLite 主库/WAL/SHM、Outbox、审计、死信、Docker 日志、trace、cliproxy 日志、`backups-test` 均无 canary；
- [ ] 合法 Authorization 正常调用；把同一 Token 复制到任一业务字段立即拒绝；无效 Token 不进入 access log、hash、异常或审计正文（未认证拒绝行仅含固定枚举与 socket peer）；
- [ ] secret query 在 audit HMAC 和 Hindsight 调用前拒绝，audit 无 query HMAC/长度；安全 query 只有带独立 key 的 HMAC+长度；
- [ ] 停用扫描器后四个 MCP 数据工具以及 worker、retry、startup recovery 全部 fail-closed；仅健康端点可报告故障；
- [ ] 安全提交后、worker 前人工注入 canary，SHA/二次扫描阻止外发；
- [ ] mock Hindsight 返回含 canary 错误 body，日志/死信/status/审计不记录；
- [ ] 在 Phase 0 disposable 链路用一次性合成凭证和只输出布尔判据的内存 mock，分别验证 Hippocampus→Hindsight 与 Hindsight→cliproxy 只有允许的安全索引内容，无 Detail、完整 MD、full MD hash 或认证秘密；不得保存 raw capture、Header、body 或精确 prompt；正式/commissioning 真实凭证环境禁止抓包；
- [ ] Hindsight 返回结果经过字段 allowlist、URI/project 一致性和最终秘密扫描，异常结果被丢弃且不回显；
- [ ] 占位符与无实际值的外部秘密引用可通过；
- [ ] 任何标签、客户端、编码、分片均不能绕过；
- [ ] secret/state/audit export 权限、Git 跟踪、shell history、进程 argv、Compose 输出均无实际凭证。
- [ ] JSON-RPC batch、压缩/超限 body、重复 key、非法 Content-Type，以及 method/id/tool name/未知键 canary 全部整体拒绝；HTTP/JSON-RPC 响应、SQLite/WAL/SHM、audit 与框架日志无原值，疑似秘密 id 只返回 `id:null`；

## 10.4 检索

- [ ] 固定 30–50 条测试集与 ground truth；
- [ ] 中文近义 3–5 组、中英混合查询进入 Top 5；
- [ ] 记录实际请求，确认 `budget=low,max_tokens=2048` 已下发，MCP 独立裁剪最多 5 卡；
- [ ] 精确错误串、IP、端口、UUID、主机名可命中；
- [ ] 事件时间故意早于导入时间，replace 重放或重建后时间召回仍锚定 `event_at`；无 `event_at` 的记忆不被错误锚定为导入时间；
- [ ] 卡片 `event_at` 只取经后置校验的 metadata 字符串；Hindsight 推断的 `occurred_start/occurred_end/mentioned_at` 不得覆盖或冒充；
- [ ] 一份 retrieval_text 被 Hindsight 提取成多个 fact 时，逐条校验后按 document_id 归并为一张卡；同组 metadata/URI/tags 冲突整组丢弃，最终 Top 5 是 5 个唯一文档而非 5 个 fact；
- [ ] project 过滤使用 `tag_groups` strict：不同 project 不串扰，未打标/错标/多 project 条目不混入；
- [ ] `global` 只有显式授予 readable_projects 的客户端在显式查询 `project=global` 时可命中；不会自动混入其他 project；未授予 writable_projects 的客户端不能 commit；
- [ ] A 客户端 commit 后 B/C 在同一授权 project 能进入 Top 5 并 read；`src:A` 保持不变，默认 search 不添加当前客户端 source 过滤；
- [ ] 50 条规模记录 search p50/p95，目标 p95 ≤ 5 s；实测不满足则记录基线并重新批准阈值或按 [06 §6.5](06-deployment.md) 记录为 Phase 3 评估项，不得静默放行；
- [ ] `simple` lexical 不足只记录为 Phase 3 评估，不在正式 Bank 原地换后端。

## 10.5 故障降级

- [ ] 停 Hindsight/PG 后 MCP 仍启动，commit/read/status 可用；
- [ ] Hindsight 停止时 search 返回明确可重试错误；
- [ ] 恢复后积压按单飞顺序自动完成；
- [ ] SQLite/registry/audit 不可用时四个 MCP 数据工具全部在任何文件/Hindsight 操作前失败；`healthz` 仍可用，`readyz` 为 503；
- [ ] 扫描器不可用时四个 MCP 数据工具全部拒绝，不因旧 SHA、只读/status 参数或 Hindsight 缓存而放行；`healthz` 仍可用，`readyz` 为 503；
- [ ] audit insert/终态更新失败分别通过 failpoint 验证，不出现成功但零审计的调用；
- [ ] search/read/status 终态 audit update 失败时结果被丢弃并返回 `HIPPOCAMPUS_AUDIT_UNAVAILABLE`；commit 已落盘时如实返回 recovery_pending，重试收敛到同一事件；
- [ ] `readyz`：Hindsight 故障 200+degraded；扫描器/Vault/SQLite/audit 故障 503；
- [ ] Agent 当前任务不因记忆中枢故障而失败。

## 10.6 客户端与认证

- [ ] 三客户端独立 Token（`HIPPOCAMPUS_CLAUDE_TOKEN`/`HIPPOCAMPUS_CODEX_TOKEN`/`HIPPOCAMPUS_OPENCLAW_TOKEN` 或等价独立引用），变量名、值和 hash 均不同，服务端只存 HMAC；
- [ ] 三客户端 Token 与 `HINDSIGHT_INTERNAL_TOKEN`、`CLIPROXY_KEY`、数据库密码、token pepper、audit key 均不复用；
- [ ] idempotency HMAC key 与上述所有秘密也不复用；registry bootstrap/issue/rotate 只从 stdin/0600 文件接收秘密，revoke 按 client_id 生效，任何管理命令的 argv/stdout/日志无 Token/hash；
- [ ] 无 Token 的 MCP initialize/tools/list 为 401；错 Token 401/403；
- [ ] Token 互换、到期、撤销测试通过；单 Token 撤销不影响另两个，`src:*` 仍按实际 Token 映射；
- [ ] Token A 无法 search/read/status 未授权 project；filter 不能扩大权限；
- [ ] 越权 project/type 与 Unicode 混淆/分隔符/路径穿越输入被拒；自报 document_id/URI/tags/trust/scope/sensitivity 返回保留字段错误；
- [ ] 客户端不能直连 Hindsight；
- [ ] Codex 用原生 HTTP + `bearer_token_env_var`；Claude 配置只引用环境变量；OpenClaw 路径已记录、固定、可回滚；
- [ ] 在真实 Claude Code、Codex、`.2.3` OpenClaw 上分别完成 `initialize→tools/list→四工具`，验证 client/source 映射和跨端共享；curl/SDK 测试不能替代；
- [ ] OpenClaw 原生能力不足时 Phase 2 确实停止，没有安装 adapter/skill；
- [ ] OpenClaw 初始 MCP 配置与 PoC→正式 Token 轮换都能热加载；旧 Token 立即拒绝、新 Token 成功且进程未重启。任一必须重启时实施确实停在能力报告；
- [ ] 应用层只接受记录的精确客户端源地址集合；PoC Token 验收后轮换；长期使用前已另行完成内部 TLS，或用户明确接受 LAN HTTP 残余风险；
- [ ] listener/Token 交付前已从有效 `client_sources` 加载非空精确并集 gate，且每个 client 有精确 peer 绑定；空集合、CIDR/网段、通配、hostname、自动学习、候选与实际 peer 不一致均 fail-closed；每个 Token 从自身 peer 成功、从另一已允许 peer 因 client→peer 不匹配被拒；source 变化需重新授权并轮换对应 Token；
- [ ] `.2.41` CLI Gateway 未作为任何配置、路由、模型/MCP endpoint 或测试流量目标；验收报告只允许记录"未参与"的排除结论，不含其配置或运行信息。

## 10.7 审计

- [ ] 每个已认证 MCP HTTP request（含 lifecycle、拒绝/错误）在 audit 表恰一行；operation/tool 只取 allowlisted enum；commit 的 request_id 可经 root_request_id/event_id 关联 Outbox；重复 request_id 不产生第二行；
- [ ] 并集 gate 拒绝与认证失败各恰有一行**未认证终态 audit**：`client_id` 为空、`source_ip`=socket peer、`state=rejected`、outcome 为 `source_denied`/`auth_failed`，不含所试 Token/Header/`X-Forwarded-For`；已认证行 `source_ip` 恒为空；SQLite 不可写时该两类拒绝确认无行且请求被拒；
- [ ] 分别触发非法 UUID、越权 project/type、保留字段、自定义 filter/status、scanner unavailable，并把 canary 放入 JSON-RPC method/id/tool name 与未知字段名；每个已认证调用均恰有一条脱敏 audit 行，只含安全错误码和 schema 枚举路径；未知工具只记 `invalid_tool`、未知键只记 `unknown_field`，且 SQLite 主库/WAL/SHM、日志、Vault/Outbox/Hindsight 均无 canary；
- [ ] 审计不含正文、title/summary/retrieval_text、query 明文、Header、Token、异常原文；安全 query 仅 audit-key HMAC+长度；
- [ ] 秘密拒绝的审计只含规则类别与字段路径；
- [ ] audit started/prepared/stored_pending/终态 failpoint 与进程 crash 后，commit、search/read/status、lifecycle 和 replay 的原行均收敛到正确安全状态；无法证明返回的非 commit 行为 `interrupted/AUDIT_FINALIZE_LOST`；
- [ ] audit sink 不可写时工具 fail-closed；
- [ ] 轮转/保留生效；JSONL 导出同样脱敏，目录/文件为 0700/0600，并发导出不阻塞或破坏 Outbox。

## 10.8 资源与现网边界

- [ ] 连续 60 分钟混合负载（每分钟 ≥10 次 search、≥4 次 read、≥2 次 1–10 KiB commit、≥4 次 status，三客户端身份轮换）下三容器无 OOM、异常重启或 healthcheck 失败；
- [ ] 峰值内存不破 3 GiB / 1 GiB / 512 MiB；
- [ ] Pi5 无持续热降频（`vcgencmd get_throttled` 全程无 throttling 置位），现有业务容器零重启且 healthcheck 保持 healthy；
- [ ] 仅 `192.168.2.41:8888` 为新增监听；DNS、Caddy、防火墙、路由、现有 network 未变；
- [ ] Compose project=`hippocampus`，service/network/volume/mount/labels 与 [06](06-deployment.md) inventory 精确一致；backend 为无主机发布但 `internal:false` 的 bridge，Hindsight 可达指定 cliproxy，db/Hindsight 无主机端口；
- [ ] 未新建 Pi5 主机账户，未改 `/etc/passwd`/`/etc/group`；
- [ ] Docker socket owner/group/ACL、`docker` group 成员和远程 daemon 均已只读核对且只有受信管理者；三个容器均未挂载 Docker socket 或等价管理 API；
- [ ] 三容器无 privileged/host namespace/非必要 device；MCP/Hindsight 的固定 UID/GID、只读 rootfs、no-new-privileges、cap_drop、tmpfs、healthcheck、日志轮转和内存硬上限与渲染配置一致；
- [ ] 仅用 `docker inspect --format` 的非秘密字段 allowlist 验 runtime user、精确 mount target、ports、cap/security、restart 与资源；无未过滤 inspect、`.Config.Env` 或 secret-bearing Compose 输出；
- [ ] MCP loopback 可调用 `/healthz`，但 loopback 未被加入 `/mcp/` source gate；未知 LAN source 也不能借健康路由访问数据协议；
- [ ] Phase 0 只使用版本化根与 `hippocampus-v4-2-phase0-<run_id>` 对象（标签 `plan=v4.2-phase0`）；计划/派生物 manifest digest 一致，未知或旧版产物会停止；Pi5 零写入，清理未触及其他 Docker 对象；
- [ ] 未创建 Gotify app/token/webhook、未发送通知；未调用 Backup API、未改 timer、未写/挂载 `/srv/backup`；
- [ ] 未安装 OpenClaw adapter/skill，未探测/调用/读取/修改/重启 Claude CLI Gateway；
- [ ] `backups-test` 只含合成数据的一次性人工导出与临时恢复。
- [ ] 三客户端配置备份本体与本地完整性摘要仅在各自 0700/0600 非 Git 安全位置；报告只有随机安全备份 ID、路径、时间、权限、`integrity_verified` 布尔值与回滚结果，无内容、diff、hash/HMAC、大小或其他指纹；
- [ ] `rollback-server.sh` 在 disposable 环境通过双清单演练：正式回滚清单对 disposable 环境拒绝，run 级 disposable 清单 dry-run/execute 通过，绝无 `down -v`，命名卷与数据保留；`rollback-clients.sh` 在合成配置及三端实际备份上完成可逆演练，输出无正文/diff/凭证；

## 10.9 Commissioning 收口

- [ ] Go 前所有三端写入都只使用 `project=commissioning`、隔离 Bank `commissioning-v4-2` 和合成数据；正式 `main`、实际项目与 `global` 保持空；
- [ ] 三端真实 E2E、跨端共享、撤销/到期/互换和 source 映射均通过；
- [ ] 验收后按 client_id 撤销/轮换三端 PoC Token，移除普通客户端 commissioning read/write，把服务端模式切到空的正式 `main`；隔离 Bank/测试 project 默认保留为不可见回归集且不参与正式 recall/consolidation；
- [ ] 经批准的 registry bootstrap manifest 才显式授予实际 readable/writable projects；`global` 未被隐式授予；新 Token 三端身份复验通过。

## 11. Go / No-Go

全部满足才允许三客户端写正式 `main`：

1. Hindsight 与 PostgreSQL 镜像已固定 ARM64 digest；
2. retain 主/备、embedding、reranker 与 failover 实测通过；reflect 不作为一期依赖；
3. 正确 `items[]+async=false` retain、RFC3339/显式 `"unset"` 时间语义、metadata event_at 与 replace 重放已验证；
4. `STORE_DOCUMENT_TEXT=false` server+Bank、`AUDIT_LOG=false`、`LLM_TRACE=false`、`OTEL_TRACES=false`、Control Plane disabled 与数据库/exporter/日志结果已验证；
5. reservation/Outbox/audit 的并发、file+directory fsync、全部崩溃窗口、幂等重放和 startup recovery 通过；
6. Hindsight 故障不阻断安全写入、已知 URI 读取与状态查询；
7. Hindsight 永远只接收安全索引字段，不接收 Detail、完整 MD 或 full MD hash；
8. 秘密红线全路径、零落盘、无 override 通过；
9. 四工具统一授权、strict+后置过滤、三客户端独立身份/撤销/跨端共享通过；
10. JSON-RPC ingress、audit gate/终态（含未认证拒绝行）、query/idempotency HMAC、导出权限与 sink 故障 fail-closed 通过；
11. OpenClaw 原生接入已通过；若需要 adapter，则在获得独立授权并完成其验收前为 No-Go；
12. 三端真实 commissioning、Token/source 收口完成；长期正式使用已完成内部 TLS，或用户已明确接受受信 LAN HTTP Bearer 残余风险；
13. `.2.41` CLI Gateway 完全未参与；
14. Gotify 通知、备份枢纽、公网和现有业务未被接入或改动。

任一失败即 No-Go。不得以「Agent 直连 Hindsight MCP」「先保存正文再补安全控制」「临时共享 Token」替代。

---

*v4.2 final*

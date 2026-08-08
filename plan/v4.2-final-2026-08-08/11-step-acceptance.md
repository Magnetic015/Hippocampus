# 11 · 分步验收计划（v4.2）

> 属于 [Hippocampus 实施计划 v4.2](00-overview-v4.md) · final · 2026-08-08

本文件把 [08-phases.md](08-phases.md) 的**每一步**映射为可判定的验收标准、验证方法与证据要求；[09](09-acceptance-gonogo.md) 是主题式总清单，本文件按执行顺序引用其条目编号，两者冲突时以更严者为准。

**判定通则**：

1. 每步验收结论只允许 `PASS / FAIL / STOPPED`（STOPPED=触发停止条件而中止，本身不是失败但阻断后续步骤）；标准中任一子项不满足即 FAIL。
2. 每步证据登记到 `phase0/v4.2-final-2026-08-08/reports/`（Phase 0）或验收报告（Phase 1/2），编号格式 `EV-<步骤号>-<序号>`；证据只含命令、allowlisted 输出、布尔判据与安全元数据，**永不含秘密值、Token、Header、正文或客户端配置 diff**（[01 §1.2](01-scope-boundaries.md)、[04 §4.2](04-security.md)）。
3. 前一步未 PASS 不得进入下一步；并行执行的子步骤各自独立判定。
4. Phase 3/4 无一期分步验收：其唯一门槛是「新的明确授权存在且范围明确」，任何一期步骤不得以 Phase 3/4 能力为前提。

## 11.0 全局前置门槛（进入任何 Phase 前）

| # | 步骤 | 验收标准 | 验证方法与证据 |
|---|---|---|---|
| G1 | 计划完整性 | 本版 `MANIFEST.sha256` 覆盖 00–11 全部文档且逐一校验通过 | `shasum -a 256 -c MANIFEST.sha256` 全 OK；输出存证 |
| G2 | 版本隔离 | 工作目录/引用中不存在 v4、v4.1 或更早版本文件的混用；Phase 0 根不含来源 digest 不符或未知产物 | 目录清单 + 各产物 manifest 中记录的 plan digest 与本版一致 |
| G3 | 停止条件基线 | [01 §1.6](01-scope-boundaries.md) 中**当前时点可只读判定**的环境类条件（未知目录/命名冲突、`8888`/接口、NVMe/内存、Docker 管理面、镜像 arm64 manifest、本版 MANIFEST）逐条核对为「未触发」；任一触发即 STOPPED。依赖后续产物或客户端的条件（Compose 渲染加固、secret plane 权限、source gate/绑定、CP listener、OpenClaw 能力）不在此判定，分别由 P0.8/P1.3/P1.5/P1.6/P1.7/P2.4 覆盖，不得以「尚不可判定」记为通过 | `preflight-readonly.sh` 只读输出逐条对照存证（每条标注「未触发」或「由后续步骤判定」） |

## 11.1 Phase 0（Pi5 只读；对应 08 Phase 0 前置与第 1–12 步）

| # | 对应步骤 | 验收标准 | 验证方法与证据 | 关联 09 |
|---|---|---|---|---|
| P0.0 | 前置：工作区与 disposable 身份 | 工作区为 `phase0/v4.2-final-2026-08-08/`；disposable 对象全部使用前缀 `hippocampus-v4-2-phase0-<run_id>` 与标签 `io.hippocampus.managed=true`、`io.hippocampus.plan=v4.2-phase0`；Phase 0 出站目标仅限镜像 registry、Hindsight 官方源与用户指定 cliproxy，且 cliproxy 只走推理/兼容探测接口、未调用任何配置或管理端点；探测端点非 `.2.41` Gateway；Pi5 全程零写入 | 目录/`docker ps --filter label` 清单；出站目标与探测命令清单；端点记录；Pi5 只读命令清单 | §10.8 |
| P0.1 | 步骤 1：契约冻结 | Markdown schema、URI 映射、四工具输入输出、reservation/Outbox/audit 状态机与错误码、审计字段（含未认证拒绝行）全部落为机器可读 schema/常量文件，与 [03](03-data-model.md)/[05](05-outbox-worker.md)/[07](07-mcp-tools-audit.md) 逐项一致，无未定义值域 | 冻结文件 digest 记入 manifest；对照检查单（每个状态/错误码/字段可指到文档小节） | — |
| P0.2 | 步骤 2：秘密扫描器 | 扫描器为本地确定性组件、内嵌 `scan_policy_version=sp1`；同输入重复运行结果一致；[09 §10.3](09-acceptance-gonogo.md) 全类别 canary 100% 拒绝且零落盘；占位符/外部引用样本 100% 通过 | canary 套件运行报告（仅布尔判据与规则类别）；重复运行一致性记录 | §10.3 |
| P0.3 | 步骤 3：Hindsight 基线 | disposable Bank 中：`items[]+async=false` retain 成功；`document_id+replace` 重放后 SQL 查证无重复文档且旧 fact set 不残留；`timestamp` 规范化 RFC3339 与显式 `"unset"` 两形态数据库结果符合 R1（并实测省略 timestamp 默认当前时间）；metadata `event_at` 与顶层同值且全字符串；`tag_groups+all_strict` 过滤生效；原生 `/health` 200 | disposable 环境 API/SQL 输出（无凭证）存证 | §10.1、§10.2 |
| P0.4 | 步骤 4：泄漏面关闭 | `STORE_DOCUMENT_TEXT=false` → `documents.original_text IS NULL` 且 chunk 正文为空；`LLM_TRACE/AUDIT_LOG/DEBUG_DUMP/OTEL_TRACES` 显式关闭后 `llm_requests`、Hindsight audit、exporter/span 无请求/响应副本；`HINDSIGHT_ENABLE_CP=false` → 容器内无 9999 listener、跨容器连 9999 失败 | SQL 判据 + `ss` 输出 + 连接失败记录 | §10.1 |
| P0.5 | 步骤 5：镜像 | Hindsight 0.9.0 与 pgvector 镜像均有 `linux/arm64` manifest；digest 已固定并记录 | `docker manifest inspect` 输出 + digest 清单 | §11 条 1 |
| P0.6 | 步骤 6：模型准入 | 按 [06 §6.5](06-deployment.md) 准入表：retain 主/备各自通过 chat、structured output、retain；人为断开主成员后备真实接管且结果正确；embedding/reranker ARM64 启动并通过中文冒烟；reflect 未被验证或依赖 | 每模型逐项布尔结果 + failover 演练记录（无 prompt 原文） | §10.4、§11 条 2 |
| P0.7 | 步骤 7：测试集 | 30–50 条非秘密中文样本 + 固定 query/ground-truth 落盘并 digest 固定；抽样复扫无秘密；query 覆盖 §10.4 全部类别（近义、中英混合、精确串、时间锚定、跨端、global、串扰） | 数据集 digest + 覆盖矩阵 | §10.4 |
| P0.8 | 步骤 8：模板与 Compose 合同 | 渲染 Compose 经 allowlist 对照 [06 §6.2](06-deployment.md) inventory 全项一致；`docker compose config -q` 通过；任一秘密缺失时 `:?required` 启动失败实测；`hindsight.env` 以 `env_file` 挂接；tiktoken `cl100k_base` 计数实现版本固定且测试向量通过（含 100/600/800 边界样例） | 渲染对照表 + 失败注入记录 + 计数向量结果 | §10.8 |
| P0.9 | 步骤 9：remote example | 三个 example 文件生成于候选目录；现网/运行配置零引用 | 文件清单 + 引用扫描（grep）结果为空 | — |
| P0.10 | 步骤 10：ingress/审计/并发/fsync | disposable MCP 上：batch/压缩/超限/重复 key/非法 Content-Type/canary 全部整体拒绝且无原值残留；audit ingress 与终态 failpoint 行为符合 [02 §2.3](02-architecture.md)（含未认证拒绝行、`AUDIT_UNAVAILABLE`、commit `recovery_pending`）；并发同 key 单 winner；[09 §10.2](09-acceptance-gonogo.md) 所列全部 failpoint 窗口恢复后无丢失/双文件/断链；failpoint 开关确认不存在于正式镜像构建；Claude/Codex 真实客户端全程无 batch 请求 | 拒绝矩阵 + failpoint 运行报告 + 正式镜像构建审计 + 客户端流量记录（仅 method 枚举计数） | §10.2、§10.3、§10.5、§10.7 |
| P0.11 | 步骤 11：source 候选 | Mac→Pi5 候选 source 地址与路由依据记录完整，且明确标注「非授权、待 Phase 1 socket peer 确认」 | 候选记录文档 | §10.6 |
| P0.12 | 步骤 12：回滚演练 | 正式回滚清单对 disposable 环境执行 → 因 project/labels 不匹配拒绝（退出码非 0）；run 级清单 `--dry-run` 与 `--execute` 通过且命令审计无 `-v`；down/up 后命名卷数据仍在；`rollback-clients.sh` 合成演练输出无正文/diff/凭证 | 双清单演练记录 + 卷数据前后对比（安全判据） | §10.8 |
| P0.X | 出口 | P0.0–P0.12 全 PASS；disposable 对象已按精确名称清理、持久产物保留并 digest 固定；Pi5 仍零写入 | 清理前后 `docker ps -a`/volume 清单对比；产物 digest 清单 | §10.8 |

## 11.2 Phase 1（Pi5 LAN 最小闭环；对应 08 Phase 1 第 1–10 步）

| # | 对应步骤 | 验收标准 | 验证方法与证据 | 关联 09 |
|---|---|---|---|---|
| P1.1 | 步骤 1：停止检查复跑 | [01 §1.6](01-scope-boundaries.md) 全部「未触发」；原始 Compose inventory、8888 占用、Docker socket/group/ACL/context/`DOCKER_HOST`、批准管理者清单已存档 | 只读预检输出 + 受信主体清单 | §10.8 |
| P1.2 | 步骤 2：目录与账户 | `/home/kkp/hippocampus/`、`/home/kkp/.config/hippocampus/` 建立且 `0700/0600`；未新建主机账户、未改 `/etc/passwd`/`/etc/group` | `stat` 输出 + passwd/group 前后对比为空 | §10.8 |
| P1.3 | 步骤 3：secret plane | `--env-file` 仅插值实测（容器 env 无未映射变量）；每秘密逐项映射；容器探针只报缺失变量名；报告零实际值；三容器各自只见自身秘密 | `docker inspect --format` allowlist 字段 + 探针布尔输出 | §10.6、§10.8 |
| P1.4 | 步骤 4：registry 初始化 | `mac-claude`/`mac-codex` 建立、`dockerNode-openclaw` disabled；三者仅授 `commissioning` read/write；Token 只存 HMAC；一次性 0600 文件未交付；CLI 全程 argv/stdout 无秘密 | `registry-list-safe.sh` 输出 + 命令审计 | §10.6 |
| P1.5 | 步骤 5：绑定与 gate | 两个 client→peer 精确绑定生效；并集 gate 非空且仅含精确 IP；人为删除绑定/清空 gate 后服务启动失败实测；规则装载先于 8888 listener（启动顺序日志） | 绑定/gate 快照 + 故障注入启动记录 | §10.6 |
| P1.6 | 步骤 6：部署 | `-p hippocampus` 三容器 up 且 healthcheck 通过；主机新增监听仅 `192.168.2.41:8888`；无 9999；labels=`plan=v4.2`；渲染配置与 inventory 一致 | `ss` 前后对比 + inspect allowlist 对照 | §10.8 |
| P1.7 | 步骤 7：Bank 与服务链路 | `main` 与 `commissioning-v4-2` 均存在且为空；两 Bank 显式 `store_document_text=false`、`audit_log_enabled=false` 且启动读取复核；commissioning mode 实测路由隔离 Bank；四工具、Outbox、单实例 worker、startup recovery、audit 全部就位；停 Hindsight 后 MCP 仍可启动（无硬依赖） | Bank config 读取记录 + 试写路由验证（合成数据） + 降级启动实测 | §10.1、§10.5 |
| P1.8 | 步骤 8：Token 交付 | 交付前 401/403 矩阵、registry HMAC、allowlist 全部通过；交付仅一次、只经 0600 文件/OS 凭证存储；交付确认后 Pi5 一次性副本已删除 | 交付前检查单 + 删除确认 | §10.6 |
| P1.9 | 步骤 9：数据集分配 | 三客户端子集划分 manifest 固定（合计 30–50 条）；尚未导入任何数据；`main`、实际 project 与 `global` 均为空且不可写 | manifest digest + Bank/Vault 空态检查 | §10.9 |
| P1.10 | 步骤 10：现网不变 | Gotify/Backup API/Caddy/DNS/防火墙/现有 network 零变更（前后对比）；Gateway 未参与，仅以变更清单、配置引用扫描与流量目标记录证明，全程未探测 Gateway | 前后对比记录 + 排除性证明文档 | §10.8 |

## 11.3 Phase 2（三个代表客户端；对应 08 Phase 2 各子步骤）

| # | 对应步骤 | 验收标准 | 验证方法与证据 | 关联 09 |
|---|---|---|---|---|
| P2.1 | 前置：能力预检与备份 | 三客户端原生 Streamable HTTP MCP 能力只读确认记录在案；每项配置变更前均有安全备份**且已记录可执行的脱敏回滚方法**（命令/路径/停止条件）；备份本体与完整性摘要仅存于客户端原安全域或独立 0700/0600 非 Git manifest；报告仅含 `client_id/备份 ID/路径/时间/权限/integrity_verified/回滚结果`，无内容/diff/hash/HMAC/大小 | 能力记录 + 备份元数据清单 + 回滚方法登记 | §10.8 |
| P2.2 | 本机 Claude Code 接入 | 配置 schema 以现行官方文档/`--help` 复核记录；配置文件零明文 Token（仅环境变量引用）；真实客户端 `initialize→tools/list` 成功且 client/source 映射正确 | 配置引用扫描 + 服务端 audit 行核对 | §10.6 |
| P2.3 | 本机 Codex 接入 | 原生 HTTP + `bearer_token_env_var`；Token 环境变量对实际运行进程可见验证通过；未回退明文；`initialize→tools/list` 成功 | 进程环境可见性验证记录 + audit 行核对 | §10.6 |
| P2.4 | OpenClaw（`.2.3`）接入 | 顺序满足：只读能力确认（原生 HTTP MCP、Bearer 安全引用、配置与 Token 双热加载）→ peer 入 gate 并绑定 `dockerNode-openclaw` → 启用并交付独立凭证 → 最小配置热加载；全程 OpenClaw 进程未重启（启动时间不变）；未装任何 adapter/skill/自动 retain 插件；配置变更前后 allowlist 对比：唯一差异为 Hippocampus MCP server 条目及其凭证安全引用，模型路由、其他服务与既有插件配置不变。若任一能力缺失：实施停在能力报告，判 STOPPED 且后续步骤不执行 | 能力报告 + 进程启动时间前后对比 + gate/绑定快照 + 配置前后对比（仅安全元数据） | §10.6 |
| P2.5 | 三端真实 commissioning E2E | 三端各自在真实运行上下文完成 `initialize→tools/list→memory_commit→memory_status→memory_search→memory_read` 全链路；分配子集全部经 `memory_commit` 导入（合计 30–50 条）；全部落 `project=commissioning` + `commissioning-v4-2`（服务端记录）；跨端共享验证（A 写、B/C search+read）；人为制造记忆失败后客户端当前任务不受阻；curl/SDK 未替代任何一端 | 三端会话记录 + 服务端 audit/Bank 归属核查 + 共享矩阵 | §10.4、§10.6、§10.9 |
| P2.6 | 行为规范抽查 | 抽查三端 E2E 会话：search ≤5 卡、read ≤1–2 篇、commit 含 summary/retrieval_text 且 event_at 可靠或 `unset`、重试复用原 key、被拒后安全改写不回显 | 会话抽查记录对照 audit | §10.7 |
| P2.7 | 前置验收总检 | [09 §10.1–§10.8](09-acceptance-gonogo.md) 逐条 PASS（含 10.2 延迟门槛、10.4 检索指标、10.8 资源与现网边界） | 09 清单逐条勾验 + 证据编号 | §10.1–10.8 |
| P2.8 | Commissioning 收口 | 按序完成并逐项实测：三端 PoC Token 撤销/轮换（旧拒新通）；普通客户端 `commissioning` read/write 移除（访问被拒实测）；服务端模式切到空 `main`（空态验证）；经批准 bootstrap manifest 显式授予实际 projects，`global` 未被隐式授予（未授权查询被拒）；新 Token 三端身份复验、OpenClaw 无重启轮换复验 | 收口操作与实测记录 | §10.9 |
| P2.9 | Go / No-Go 评估 | 评估前复验正式零写入：`main` Bank、实际 project 与 `global` 的 Bank/Vault/Outbox 均无任何耐久提交，仅 `commissioning-v4-2` 与 `shared/commissioning/` 含合成数据；随后按 [09 §11](09-acceptance-gonogo.md) 十四条逐条判定并记录依据；全部满足才 Go；TLS 或 LAN 残余风险已获用户明确决定 | 正式零写入复验记录 + Go/No-Go 决议文档（逐条引用证据编号） | §11 |

## 11.4 Phase 3 / Phase 4

无一期分步验收。唯一门槛：**新的明确授权**（范围、对象、回滚均在授权中列明）。在授权出现前，任何 Phase 3/4 条目（reindex/reconcile、consolidation、Gotify、备份枢纽、TLS 例外之外的远程接入等）出现在实施或验收记录中即为越界，按 [01 §1.6](01-scope-boundaries.md) 停止。

---

*v4.2 final*

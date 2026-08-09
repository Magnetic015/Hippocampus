# 计划完成度回溯与颗粒度对齐（v4.2）

> 属于 [Hippocampus 实施计划 v4.2](../../../plan/v4.2-final-2026-08-08/00-overview-v4.md) · 回溯日期 2026-08-08
> 依据:计划 [`09-acceptance-gonogo.md`](../../../plan/v4.2-final-2026-08-08/09-acceptance-gonogo.md)(§10.x + 14 点 Go/No-Go)、[`11-step-acceptance.md`](../../../plan/v4.2-final-2026-08-08/11-step-acceptance.md)(G/P0/P1/P2 分步)对齐四份执行报告(`phase0-report`、`phase1-pi5-deployment`、`phase2-clients`、`phase2-load-test`)+ git 提交 + Pi5 现网 + 本会话新增。
> 图例:✅ 完成 · 🟡 部分/进行中 · ⬜ 未开始/未决 · ⚠️ 偏差(已接受) · **相位漂移**=计划归属相位 ≠ 实际落地相位。

## A. 完成度总览

| 分块 | 计划条目 | 达成 | 结论 |
|---|---|---|---|
| 全局门槛 G1–G3 | 3 | ✅ 3 | 完成(G3 实际在 P1.1 判定) |
| Phase 0 P0.0–P0.12 | 13 | ✅ 13(内容) | **验收内容全部满足,但 6 项相位漂移到 P1/P2 才达成** |
| Phase 1 P1.1–P1.10 | 10 | ✅ 10 | 完成(含 3 处已接受偏差) |
| Phase 2 P2.1–P2.9 | 9 | ✅3 🟡2 ⬜4 | **进行中(~40%)** |
| 主题 §10.1–§10.9 | 9 | ✅5 🟡2 ⬜/✗2 | §10.4 检索未达标、§10.9 收口未做 |
| Go/No-Go 14 条 | 14 | ✅~11 🟡1 ⬜2 | **未评估;当前 No-Go** |

**总判**:Phase 0/1 实质完成;Phase 2 客户端接入过半(Claude 今日真实跑通、OpenClaw 已接入),但**三端真实 E2E + 数据集导入、commissioning 收口、§10.4 检索延迟决策、TLS/LAN 残余风险决策**未完成 → Go/No-Go 未评估、当前不满足。

---

## B. 全局门槛（11.0）

| # | 步骤 | 状态 | 证据 / 说明 |
|---|---|---|---|
| G1 | 计划完整性 | ✅ | `MANIFEST.sha256` 12/12 校验 OK(phase0 §3) |
| G2 | 版本隔离 | ✅ | 工作产物无 v4/v4.1 混用(phase0 §3);注:`plan/v4.1-revised-*/` 作为历史保留,不参与产物 |
| G3 | 停止条件基线 | ✅ ⚠️ | **相位漂移**:Phase 0 记 "N/A 本轮",实际在 **P1.1** 判定(12/13 未触发,1 项 beszel 已接受) |

---

## C. Phase 0（11.1，P0.0–P0.12）

> Phase 0 本地无 Docker(`command not found`),故 6 项在本轮记 OPEN,后于真实基建补齐 → **相位漂移**。自动化检查 **132 项通过**(phase0 §1;注:PR #1 正文写作 "133",应以报告 132 为准)。

| # | 步骤 | 本轮 | 现状 | 证据 / 说明 |
|---|---|---|---|---|
| P0.0 | 工作区+disposable 身份 | ✅ | ✅ | Pi5 零写入 |
| P0.1 | 契约冻结 | ✅ | ✅ | constants.py 冻结枚举/限额 |
| P0.2 | 秘密扫描器 sp1 | ✅ | ✅ | 14 类 canary 拒绝、确定性;**修复** CJK 边界绕过 bug(cc72ec4 内) |
| P0.3 | Hindsight 基线 | 🟡 mock | ✅ **P1** | 真实 v0.9.0 retain/replay/event_at 验证(phase1 §5) |
| P0.4 | 泄漏面关闭 | ⬜ | ✅ **P1** | `original_text` NULL、`llm_requests`=0、无 9999(phase1 §5) |
| P0.5 | 镜像 arm64+digest | ⬜ | ✅ **P1** ⚠️ | hindsight digest 固定;**pgvector 偏差**:`pg18` 浮动标签不存在→`0.8.1-pg18` 固定 digest |
| P0.6 | 模型准入+failover | ⬜ | ✅ **P1** ⚠️ | **模型偏差**:主 `gemini-3-flash`→(deepseek-v4-flash)→`gpt-5.6-terra`(bc9cf8b);failover 真实接管(phase1 §6.4) |
| P0.7 | 测试集 | ✅ | ✅ | 32 中文样本 + 20 query,3 端 11/11/10 |
| P0.8 | 模板+Compose 合同 | 🟡 | ✅ **P1** | Compose `config -q` 通过、`:?required` 缺失即失败(phase1) |
| P0.9 | remote example | ✅ | ✅ | 3 例、运行配置零引用 |
| P0.10 | ingress/审计/并发/fsync | ✅ | ✅ | 全崩溃窗口恢复、单 winner;failpoint 仅测试镜像 |
| P0.11 | source 候选 | ⬜ | ✅ **P1** | P1.5 peer 绑定(mac .2.46 / openclaw .2.3) |
| P0.12 | 回滚演练 | 🟡 | ✅ **P2** | 容器级演练在 disposable project(phase2-clients §6):错配拒绝、`down` 无 `-v`、卷存活 |
| P0.X | 出口门槛 | — | ⚠️ | **过程偏差**:计划要求「前一步未 PASS 不进入下一步」,实际带 6 项 P0 OPEN 进入 Phase 1(因本地无 Docker),后补齐 |

---

## D. Phase 1（11.2，P1.1–P1.10)—— ✅ 全部完成

| # | 步骤 | 状态 | 证据 / 说明 |
|---|---|---|---|
| P1.1 | 停止检查复跑 | ✅ ⚠️ | 12/13 未触发;1 触发(Docker 管理面含 beszel)→ **已接受**并入受信主体 |
| P1.2 | 目录与账户 | ✅ | 0700/0600;passwd/group 前后一致 |
| P1.3 | secret plane | ✅ | `--env-file` 仅插值、逐秘密映射、三容器各见自身 |
| P1.4 | registry 初始化 | ✅ | mac-claude/mac-codex;openclaw disabled;仅 commissioning 读写 |
| P1.5 | 绑定与 gate | ✅ | 精确 IP 并集 gate、client→peer 绑定;先于 8888 装载 |
| P1.6 | 部署 | ✅ | 三容器 healthy;仅新增 `192.168.2.41:8888` |
| P1.7 | Bank 与链路 | ✅ | `main` 空 + `commissioning-v4-2`;bank 级锁;停 Hindsight 仍启动 |
| P1.8 | Token 交付 | ✅ | 一次性 0600;交付前 401/403+HMAC+allowlist 通过 |
| P1.9 | 数据集分配 | ✅ | manifest 11/11/10 固定;**按计划尚未导入** |
| P1.10 | 现网不变 | ✅ | 5 个既有容器零重启;Gateway 未参与 |

---

## E. Phase 2（11.3，P2.1–P2.9)—— 🟡 进行中

| # | 步骤 | 状态 | 证据 / 说明 |
|---|---|---|---|
| P2.1 | 能力预检+备份 | ✅ | 三端原生 HTTP MCP 能力确认;备份 `BK-fc60e4a8`/`BK-e4d6ba3a`(0600) |
| P2.2 | 本机 Claude Code 接入 | ✅ **本会话** | 今日经分身(Claude-2)真实 `memory_search`+`memory_read` 跑通;**依赖 `_meta` 修复(6ebd8f0)才可用** |
| P2.3 | 本机 Codex 接入 | ⬜ | token 方案就绪,真实运行 E2E 未验 |
| P2.4 | OpenClaw(.2.3)接入 | ✅ | mcp probe 4 工具、peer 绑定、进程未重启、无 adapter(phase2-clients §3/§4) |
| P2.5 | 三端真实 commissioning E2E | 🟡 | Claude 端 search/read 已验;**commit 导入、跨端共享、故障注入、Codex/OpenClaw 全链未做;30–50 条数据集未导入** |
| P2.6 | 行为规范抽查 | ⬜ | 依赖 P2.5 会话 |
| P2.7 | 前置验收总检(§10.1–10.8) | 🟡 | 多数通过;**§10.4 延迟不达标、§10.8 欠压位、§10.4 中文召回未验** → 未全 PASS |
| P2.8 | Commissioning 收口 | ⬜ | 撤销/轮换 Token、移权限、切空 `main`、按 manifest 授权实际 project——未做 |
| P2.9 | Go/No-Go 评估 | ⬜ | 未评估 |

---

## F. 主题验收（§10.1–§10.9）

| 主题 | 状态 | 证据 / 说明 |
|---|---|---|
| §10.1 数据分层 | ✅ | `documents.original_text`=NULL、`llm_requests`=0、无 9999、canary 零落盘(phase1 §5) |
| §10.2 Outbox/幂等 | ✅ | 全崩溃窗口恢复(phase0 test build);commit p95 54ms « 500ms 门槛;prod 无 failpoint 代码(设计) |
| §10.3 秘密红线 | ✅ | 19 扫描器用例 + 现网 canary 零落盘 + 1 `secret_rejected`(phase1 §5) |
| §10.4 检索 | ⬜ ✗ | **search p95 11.3s > 5s 目标**(reranker 瓶颈,已记基线待决);**中文召回质量未验**(需真实导入) |
| §10.5 故障降级 | ✅ | 停 Hindsight 降级启动、fail-closed 路径验证 |
| §10.6 客户端与认证 | 🟡 | 认证矩阵/peer 绑定/撤销 ✅;**三端真实 E2E + 跨端共享未完成**(Codex 待、共享未验) |
| §10.7 审计 | ✅ | 24 行/24 request_id;未认证拒绝行 client_id 空+source_ip 有 |
| §10.8 资源与现网边界 | 🟡 ⚠️ | 60min/940 请求/0 错误、峰值 115/1670/94 MiB « 上限、零重启 ✅;**唯 `vcgencmd` 欠压位 `0x50000` 使"全程无限频"字面未满足**(环境残留,建议 27W PD) |
| §10.9 Commissioning 收口 | ⬜ | 未开始 |

---

## G. Go / No-Go 14 条（§11）

| # | 条目 | 状态 | 说明 |
|---|---|---|---|
| 1 | ARM64 digest 固定 | ✅ | hindsight digest;pgvector 替代并固定 |
| 2 | retain 主/备+embedding+reranker+failover | ✅ ⚠️ | 含模型替代;failover 真实接管 |
| 3 | items[]+async=false+时间语义+replace | ✅ | phase1 §5 |
| 4 | 泄漏开关全关+CP disabled | ✅ | phase1 §5 |
| 5 | 并发+fsync+崩溃窗口+recovery | ✅ | phase0 test build |
| 6 | Hindsight 故障不阻断安全写/读/status | ✅ | phase1 §5 |
| 7 | Hindsight 只收安全索引字段 | ✅ | `documents=2, original_text=0` |
| 8 | 秘密红线全路径零落盘 | ✅ | — |
| 9 | 四工具授权+三端身份/撤销/**跨端共享** | 🟡 | 授权/身份/撤销 ✅;**跨端共享未验**(依赖 P2.5) |
| 10 | JSON-RPC ingress+审计+HMAC+sink fail-closed | ✅ | — |
| 11 | OpenClaw 原生接入 | ✅ | 无 adapter |
| 12 | 三端收口 + 内部 TLS **或** 用户接受 LAN 明文 | ⬜ | 收口未做;**TLS/LAN 决策未定** |
| 13 | `.2.41` Gateway 未参与 | ✅ | — |
| 14 | Gotify/备份/公网/现有业务未动 | ✅ | — |

**当前 ~11/14 满足;阻断项:#9(跨端共享)、#12(收口 + TLS/LAN 决策),叠加 §10.4 延迟(经 P2.7)。→ 未评估、当前 No-Go。**

---

## H. 计划 vs 实际——颗粒度错位与对齐建议

1. **相位边界漂移**(最主要):`P0.3/0.4/0.5/0.6/0.8/0.11` 计划归 Phase 0,因本地无 Docker 实际在 **Phase 1** 达成;`P0.12` 在 **Phase 2** 达成。计划的「前一步未 PASS 不进入下一步」被放宽。
   → **对齐建议**:在 `11-step-acceptance.md` 为这些 P0.x 增补"实际达成相位 + 证据出处"列,或把"需真实基建"的 P0 子项显式标注为"Phase 1 联合验收项"。

2. **证据编号制未落地**:计划 11 §通则要求 `EV-<步骤号>-<序号>` 证据编号,四份报告**全无** `EV-` 编号(改用 PASS/PARTIAL/OPEN 表 + `BK-*`/digest/`request_id` 计数)。
   → **对齐建议**:二选一——回填 `EV-` 编号,或将计划 11 的证据格式改判为报告实际采用的"分步状态表"约定(更省事且已成事实)。

3. **步骤 ID 未回填报告**:报告按"轮次/主题"分节(phase1 §1–§8),非 `P1.x`;追溯需人工映射。
   → **对齐建议**:各报告加一张 `Pn.x → 报告小节` 索引(或以本矩阵为准)。

4. **计划未覆盖的真实缺陷**:`_meta` 信封拒绝(6ebd8f0)会让**任何合规 MCP 客户端**的工具调用全被拒——正是 `P2.2/P2.5` 真实客户端 E2E 才能暴露、curl 无法替代的问题,但计划验收项里没有"真实 MCP 客户端(带 `_meta`)往返"这一颗粒。
   → **对齐建议**:在 §10.6 / P2.2 增补子项"真实客户端(含 `_meta`/progressToken)四工具往返"(Claude 端已满足)。

5. **计数口径不一致**:PR #1 正文写"133 automated checks",phase0 报告为"132"。
   → **对齐建议**:统一为 132(或复核后统一),PR 正文已可再校。

6. **不变量一致(良好)**:`main` 空、全部合成数据在 `commissioning-v4-2`/`shared/commissioning/` —— 计划与实际一致,无需调整。

---

## I. 距 Go / No-Go 还差什么(收敛路径)

1. **P2.5 三端真实 E2E + 数据集导入**:Claude 端补 `memory_commit` 导入其 11 条子集 → Codex 接入并全链 → OpenClaw 全链;跨端共享(A 写 B/C 读)+ 故障注入不阻断。(→ 满足 #9、§10.4 中文召回、§10.6)
2. **§10.4 检索延迟决策**:调 `RERANKER_MAX_CANDIDATES`/换 rrf 达标,或重批 p95 阈值到实测基线。(→ 解 P2.7)
3. **P2.8 Commissioning 收口**:撤销/轮换三端 PoC Token(+ cliproxy key)、移普通客户端 commissioning 读写、切空 `main`、按 manifest 授权实际 project。(→ 满足 #12 前半)
4. **Go/No-Go 条 12 TLS/LAN 决策**:内部 TLS 或书面接受 LAN 明文 Bearer 残余风险。
5. **P2.9 评估**:复验正式零写入后按 14 条逐条判定。

*完成 1–4 后方可执行 5;当前处于 Phase 2 中段。*

---

## J. 收口执行与切生产（2026-08-09 更新）

按用户四项决策执行**简化收口**并切生产，全部实测：

**执行结果**
- PoC token 转**永久**（`expires_at=NULL`，三端）；registry 覆盖式授权 commissioning→`soul`（mac-claude / mac-codex 读写、dockerNode-openclaw 只读）；`HIPPOCAMPUS_MODE` commissioning→`production` + 重建 mcp（healthy）。
- 生产态校验：`main` 库 0 文档、`shared/soul` 空、`global` 未授予；mac-claude 认证 200、commissioning 访问被拒（`HIPPOCAMPUS_AUTHORIZATION_DENIED`）；peer 绑定不变。
- **Claude 端首次生产写入**：`memory://shared/soul/mem_20260808_ZAH50N1TTS2450Y5F27QMEA79M`，commit→status(`indexed`,attempt=1)→read→search(project=soul Top-1) 全链通过；`main` 现有 1 条正式文档。

**Go/No-Go 记分卡（§11）**：1–8 / 10 / 13 / 14 ✅；#2 ✅（模型偏差 gpt-5.6-terra）；#9 🟡（Claude↔Codex 跨端已验，OpenClaw 延后）；#11 ✅ 接入（写入延后）；#12 🟡（收口完成 + LAN 明文书面接受；PoC 永久非轮换）；§10.4 ⏭️ Phase 3。→ 严格十四条因两处已接受偏差非满分；用户批准**「范围内 Go」**（Claude/Codex × soul，LAN 明文，§10.4 延后，OpenClaw 延后）。

**待办（仅用户）**：cliproxy API key 轮换（曾泄露）；OpenClaw 写入 E2E（后续 .2.3 真实运行时）。
**回滚**：env `HIPPOCAMPUS_MODE=commissioning`（pepper 不动）+ 重建 mcp；grant 可覆盖回 commissioning。

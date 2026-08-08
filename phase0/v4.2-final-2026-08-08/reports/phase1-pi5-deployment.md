# Phase 1 Pi5 部署验证报告（v4.2）

> 目标：`kkp@192.168.2.41` Docker · 日期：2026-08-08  
> Compose project：`hippocampus` · 模式：commissioning（隔离 Bank `commissioning-v4-2`）  
> 计划：[plan/v4.2-final-2026-08-08](../../../plan/v4.2-final-2026-08-08/00-overview-v4.md)、分步判据见 [11-step-acceptance.md](../../../plan/v4.2-final-2026-08-08/11-step-acceptance.md)

## 1. 结论

三容器在 Pi5 上部署成功并通过真实链路验证：`initialize → tools/list → memory_commit → memory_status → memory_search → memory_read` 全链路打通，Hindsight 真实 retain 与召回可用，秘密红线、审计、授权、幂等与降级语义均在真实环境成立。

**唯一新增主机监听为 `192.168.2.41:8888`**，现网 5 个容器（gotify、gotify-caddy、igotify-assist、backup-api、homeassistant）全程未重启、状态不变，`/etc/passwd`、`/etc/group` 摘要未变，`.2.41` Claude CLI Gateway 未被探测、调用或引用。

部署过程中发现并修复了**两个只有真实环境才会暴露的实现缺陷**（§4）。

## 2. 停止条件复核（Phase 1 步骤 1）

[01 §1.6](../../../plan/v4.2-final-2026-08-08/01-scope-boundaries.md) 逐条只读核对，13 项中 12 项未触发：

| 条件 | 结果 |
|---|---|
| `/home/kkp/hippocampus/` 存在未知内容 | 未触发（目录不存在） |
| 容器名 / project / network / volume 冲突 | 未触发 |
| `192.168.2.41` 不在目标接口；`8888` 被占用 | 未触发（eth0；8888/9999 空闲） |
| 镜像无 arm64 manifest | 未触发（见 §3） |
| NVMe / 内存低于基线 | 未触发（206 GiB 可用、6.6 GiB 可用内存 vs 4.5 GiB 上限） |
| Docker / Compose 解析失败 | 未触发（29.6.1 / v5.3.1，`config -q` 通过） |
| 本版 MANIFEST 校验失败 | 未触发（12/12 OK） |
| secret plane 权限不满足 0700/0600 | 未触发 |
| **Docker 管理面含未批准主体** | **触发 → 用户裁决后接受（见下）** |
| Compose 渲染出现 privileged / host namespace 等 | 未触发 |
| 无非空精确 source allowlist | 未触发（实测 peer 绑定） |
| Control Plane 无法禁用 / 9999 listener | 未触发 |
| 需新建账户 / 覆盖 / prune / 重启现有对象 | 未触发 |

### 2.1 已接受的管理面残余风险

`docker` 组成员为 `kkp,beszel`。`beszel` 是 `/opt/beszel-agent/beszel-agent`（systemd `beszel-agent.service`，`nologin` 用户，上报至 `192.168.2.3:8090` 的监控 hub），按计划口径等价 root，技术上可读取任意容器环境，包括本部署注入的全部秘密。

用户裁决：**接受并记入受信主体清单**。

**Docker 管理面受信主体清单**：`root`、`kkp`（sudo + docker）、`beszel`（docker，监控 agent）。无 TCP daemon、无 `DOCKER_HOST`、context 为 `default`、socket 为 `root:docker 0660`、无 `daemon.json`。

**残余风险**：监控 agent 若被攻陷即可读取记忆中枢全部秘密。缓解方式（未实施，需另行授权）：将 beszel 移出 `docker` 组并改用只读 socket 代理。

## 3. 镜像与模型准入

| 组件 | 结果 |
|---|---|
| `ghcr.io/vectorize-io/hindsight:0.9.0` | arm64 manifest 存在，digest 固定 `sha256:ed9f566a…f12e58` |
| pgvector | 计划所列 `pg18` 浮动标签**不存在**；改用 `pgvector/pgvector:0.8.1-pg18`，digest `sha256:fb00e285…9ee64e`（计划本就要求固定明确 digest 而非浮动标签） |
| `hippocampus-mcp` | 本地构建，标签含源码 commit；生产阶段实测 failpoint 已剥离（`arm()` 抛 `failpoints not built`，`hit()` 惰性） |

### 3.1 retain 模型替换（计划候选未通过准入）

[06 §6.5](../../../plan/v4.2-final-2026-08-08/06-deployment.md) 的候选 `gemini-3-flash` **在该 cliproxy 上不存在**（未列于 `/models`，调用返回 502）。按准入表实测替换：

| 角色 | 计划候选 | 实测结果 | 采用 |
|---|---|---|---|
| retain 主 | `gemini-3-flash` | 不存在 / 502 | `deepseek-v4-flash`（chat 200，`json_object` 返回干净 JSON） |
| retain 备 | `deepseek-v4-flash` | `json_schema` 返回 `response_format type is unavailable` | `gemini-3.6-flash`（chat 200，接受 `json_schema`） |

裸探针显示两者都不严格遵守 `json_schema`，但 **Hindsight 真实 retain 成功**（`STREAMING RETAIN COMPLETE: 5 units in 41.452s`），说明 v0.9.0 未依赖 strict schema 模式。failover 真实接管已在 §6.4 验证通过。

## 4. 部署中发现并修复的实现缺陷

### 4.1 retain 超时预算过短（`5cee863` 之前）

Hindsight retain 在容器内触发 LLM 事实抽取，Pi5 实测约 40 秒；客户端对所有调用共用 10 秒超时，导致每次尝试都在抽取完成前放弃，下一次 attempt 重新触发一遍抽取。单条提交消耗 **4 次 attempt、3 次冗余 LLM 抽取**才到达 `indexed`。

稳定 `document_id` + `replace` upsert 保证了没有产生重复文档（Hindsight 日志 `DELTA RETAIN (no changes)`），损失仅为重复 LLM 开销。

修复：retain 与 recall 使用独立超时预算（300 s / 15 s，可由环境变量覆盖）。修复后实测 **attempt=1 一次成功**。

### 4.2 召回字段名与真实 v0.9.0 不符

`memory_search` 按 mock 的 `content` / `score` 取值，真实 v0.9.0 recall 返回的是 **`text` / `scores.final`**。结果：每张卡片 `safe_snippet` 为空、排序分全为 0.0，Top-5 顺序实际随机。mock 用了同样的臆造字段名，因此 Phase 0 测试与该缺陷"一致地错"。

修复：按真实字段名取值（兼容旧名），并把 mock 对齐真实响应结构（含一个永远不得进入卡片的 `mentioned_at`），使管道测试验证服务端真正收到的结构。修复后 snippet 与排序恢复正常。

## 5. 真实链路验收结果

| 项 | 结果 |
|---|---|
| 健康路由 | `/healthz` 200 alive、`/readyz` 200 ready、`/version` 200 |
| 认证矩阵 | 无 Token 401、错 Token 401、disabled client Token 401、有效 Token 200 |
| 生命周期 | `initialize` / `tools/list` 正常，工具集恰为四个 |
| ingress 限型 | batch 400、`text/plain` 415、`Content-Encoding: gzip` 415 |
| commit | `stored:true` / `index_state:index_pending`，URI 与 document_id 服务端生成 |
| 幂等重放 | 同 key 同 payload 复用同一 document_id/URI，审计记 `idempotent_replay` |
| status | 状态投影正确，`indexing → indexed`，attempt/next_retry 可见 |
| search | 真实 Hindsight 召回；5 个 fact 按 `document_id` 归并为 1 张卡；`event_at` 锚定提交值 `2026-08-08T15:20:00+08:00` 而非 Hindsight 推断的 `mentioned_at` |
| 授权 | 未授权 project 返回 `HIPPOCAMPUS_AUTHORIZATION_DENIED` |
| 保留字段 | 自报 `trust` 返回 `RESERVED_FIELD_FORBIDDEN` |
| 秘密红线 | 合成 canary 整笔拒绝，`fields:[detail_body]`、`categories:[credential]`，`stored:false` |
| **canary 零落盘** | SQLite 主库/WAL/SHM、Vault、三容器日志**全部无命中** |
| 数据分层 | `documents=2, with_original_text=0`（`STORE_DOCUMENT_TEXT=false` 生效）；`llm_requests` 表存在但 **0 行**（trace 关闭） |
| Control Plane | 日志 `Control Plane disabled`，容器内仅 `0.0.0.0:8888` listener，**无 9999**；进程表仅 2 个进程 |
| 审计 | 24 行 / 24 个唯一 request_id；未认证拒绝行 3 条（`client_id` NULL + `source_ip` 有值）；已认证行 `source_ip` 为 NULL 的数量为 0；`secret_rejected` 1 条；`1 event : N audit` join 成立 |
| 容器身份 | hindsight `uid=1000(hindsight)` 非 root；MCP 以 `1000:1003` 运行、只读 rootfs |
| 现网边界 | 5 个现网容器状态不变、未重启；新增监听仅 `192.168.2.41:8888`；`/etc/passwd`、`/etc/group` 摘要未变 |

## 6. Phase 1 步骤 7 / 9 收口（第二轮）

### 6.1 Bank 与配置锁定（步骤 7）

两个 Bank 均已存在并**以 bank 级显式 PATCH** 锁定，而非仅继承服务端 env 默认（后者会在日后改动 env 时静默翻转）：

| Bank | facts | `store_document_text` | `audit_log_enabled` | `enable_auto_consolidation` | `llm_requests` | Hindsight audit |
|---|---:|---|---|---|---:|---:|
| `main` | 0 | false | false | false | 0 | 0 |
| `commissioning-v4-2` | 16 | false | false | false | 0 | 0 |

配置 PATCH 的请求体需要 `{"updates": {...}}` 包装；直接提交扁平字段返回 422。

**排障记录**：首轮探测 bank 端点全部 401，原因是探针取了 `HIPPOCAMPUS_HINDSIGHT_TOKEN`——那只是 compose 里的映射名，secret plane 中的变量名是 `HINDSIGHT_INTERNAL_TOKEN`。与 API 无关。

### 6.2 数据集分配（步骤 9）

`datasets/phase1-assignment-manifest.json`：32 条分配为 `mac-claude` 11 / `mac-codex` 11 / `dockerNode-openclaw` 10。

**按计划未导入。** 本轮曾开始脚本批量导入，在第 3 条（分配给仍为 disabled 的 `dockerNode-openclaw`）正确地被 401 拦下后停止并纠正：Phase 1 步骤 9 明确要求"尚不导入"，且 [09 §10.6](../../../plan/v4.2-final-2026-08-08/09-acceptance-gonogo.md) 要求导入必须由三端**各自真实运行上下文**完成，"curl/SDK 测试不能替代"——脚本导入反而会使该项验收失效。已进入隔离 Bank 的 2 条合成记录保留为回归基线。

### 6.3 授权边界

以 schema 合规的 payload 实测（长度不足会先被 `LIMIT_EXCEEDED` 拦下，测不到授权层）：

| project | 结果 |
|---|---|
| `global` | `HIPPOCAMPUS_AUTHORIZATION_DENIED` |
| `piworkspace`（实际 project） | `HIPPOCAMPUS_AUTHORIZATION_DENIED` |
| `commissioning` | 正常写入 |

### 6.4 failover 真实接管（Go/No-Go 条 2）

把主成员指向不存在的模型名并重启 Hindsight（本项目自有容器，非现网业务），提交一条合成记忆：

```
LLM member 0 (openai/nonexistent-primary-canary) failed on call: 502; trying next member (1 left)
APIStatusError (openai/gemini-3.6-flash, scope=retain_extract_facts, attempt 1/4): HTTP 429
STREAMING RETAIN COMPLETE: 1 units across 1 batches in 18.443s
```

备成员真实接管并完成抽取，Outbox 在 **attempt=1** 到达 `indexed`。备成员首次调用撞到 429 配额后自行重试成功——该配额压力值得在 Phase 2 三端并发导入时复核。配置已还原，重启后日志无 canary 模型引用。

## 7. 仍为 OPEN 的项

| 项 | 原因 |
|---|---|
| 09 §10.2 failpoint 全崩溃窗口 | 生产镜像已剥离 failpoint（设计如此）；该验收在 Phase 0 disposable 测试构建完成 |
| 09 §10.4 中文召回质量 | 数据集按计划待 Phase 2 由三端真实导入后才能验收 |
| 09 §10.8 60 分钟负载 / 资源峰值 | 未执行 |
| Phase 2 三端真实 E2E | 未开始 |
| P0.12 容器级回滚演练 | 回滚脚本未在本 project 上执行 |
| 内部 TLS 或 LAN 明文 Bearer 残余风险决定 | 未决（Go/No-Go 条 12） |

**Go / No-Go：尚未评估。** 正式 Bank `main` 已创建但保持为空（facts=0）；全部数据在隔离 Bank `commissioning-v4-2` 与 `vault/shared/commissioning/`（6 个文档）。

## 8. 待办：凭证轮换

cliproxy 的 API key 在配置过程中经由对话通道传递，已落入会话记录。计划 [09 §10.9](../../../plan/v4.2-final-2026-08-08/09-acceptance-gonogo.md) 本就要求验收后轮换 PoC 凭证，**建议把该 cliproxy key 一并纳入轮换范围**。三客户端 Token 已设 14 天到期，`dockerNode-openclaw` 保持 disabled 且无 peer 绑定。

## 9. 回滚

```bash
bash /home/kkp/hippocampus/scripts/rollback-server.sh --manifest /home/kkp/hippocampus/scripts/rollback-manifest-prod.env --dry-run
```

清单固定 `project=hippocampus` + `io.hippocampus.managed=true` + `io.hippocampus.plan=v4.2`，不匹配即拒绝；执行不带 `-v`，命名卷 `hippocampus-pgdata`、`hippocampus-hf-cache` 默认保留。

---

*Phase 1 部署报告 · v4.2*

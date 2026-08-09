# Phase 0 执行报告（v4.2 · 本机隔离验证）

> 计划版本集：`plan/v4.2-final-2026-08-08/`（MANIFEST 12/12 校验通过）  
> 工作区：`phase0/v4.2-final-2026-08-08/`  
> 执行环境：Mac（darwin 24.6.0）、Python 3.12（uv 虚拟环境）  
> Pi5 写入：**零**（本轮未连接 `192.168.2.41`，未创建任何远端对象）

## 1. 结论摘要

Phase 0 中**不依赖 Docker 与真实 Hindsight** 的部分已全部实现并验证通过：契约冻结、秘密扫描器、四工具、reservation/Outbox/audit 状态机、lease 契约、startup recovery、JSON-RPC ingress、registry 与并集 gate、单实例 worker、模板与回滚清单机制。自动化验收 **132 项全部通过**。

依赖 Docker 守护进程、真实 Hindsight v0.9.0 镜像或 cliproxy 的步骤（P0.3 部分、P0.4、P0.5、P0.6、P0.12 容器级演练）**未执行**，状态为 OPEN，理由见 §4。本报告不把任何 OPEN 项记为通过。

## 2. 自动化验收结果

```
132 passed in 100.73s
```

| 测试模块 | 项数 | 覆盖的验收主题 |
|---|---:|---|
| `tests/test_secret_redline.py` | 19 | 09 §10.3 秘密红线全路径、零落盘、fail-closed |
| `tests/test_search_degrade.py` | 18 | 09 §10.1/§10.4/§10.5 retain 载荷、R12 归并、降级与 lease |
| `tests/test_auth_registry.py` | 16 | 09 §10.6 三端身份、并集 gate、client→peer 绑定、授权 |
| `tests/test_commit_idempotency.py` | 15 | 09 §10.2 幂等、并发、状态域、URI 与读取边界 |
| `tests/test_templates_rollback.py` | 14 | P0.8/P0.12 Compose 合同、秘密映射、双清单回滚 |
| `tests/test_ingress.py` | 14 | 07 §7.0 JSON-RPC 限型与 canary 不回显 |
| `tests/test_failpoints_recovery.py` | 12 | 09 §10.2 崩溃窗口与 05 §5.3 startup recovery |
| `tests/test_audit.py` | 11 | 09 §10.7 一请求一行、审计先于响应、保留期清理 |
| `scanner/test_canary.py` | 7 | sp1 扫描器 canary 与确定性 |
| `tests/test_dataset_e2e.py` | 6 | P0.7 数据集三端导入与检索管道 |

复现命令：

```bash
cd phase0/v4.2-final-2026-08-08/hippocampus-mcp && HIPPOCAMPUS_TEST_BUILD=1 .venv/bin/python -m pytest tests/ ../scanner
```

## 3. 分步判定（对照 [11-step-acceptance.md](../../plan/v4.2-final-2026-08-08/11-step-acceptance.md)）

| 步骤 | 判定 | 依据 |
|---|---|---|
| G1 计划完整性 | **PASS** | `shasum -c MANIFEST.sha256` 12/12 OK |
| G2 版本隔离 | **PASS** | 产物仅位于本版根；无 v4/v4.1 文件引用 |
| G3 停止条件基线 | **N/A（本轮）** | 未进入 Pi5 只读预检；Phase 1 前执行 |
| P0.0 工作区与 disposable 身份 | **PASS** | 工作区路径正确；未创建任何 Docker 对象；未访问 cliproxy 与 `.2.41` Gateway；Pi5 零写入 |
| P0.1 契约冻结 | **PASS** | `constants.py` 冻结全部枚举/上限/状态域；`test_commit_idempotency.py::test_state_check_constraints_enforced` 证明 CHECK 生效 |
| P0.2 秘密扫描器（sp1） | **PASS** | 14 类 canary 全部拒绝；占位符/外部引用全部放行；重复运行结果一致 |
| P0.3 Hindsight 基线 | **PARTIAL** | `items[]`+`async=false`、`document_id`+`replace` 无重复、RFC3339 与显式 `unset` 两形态、`tag_groups` strict 已用 **mock** 验证；真实 v0.9.0 与 PostgreSQL 未验证 → OPEN |
| P0.4 泄漏面关闭 | **OPEN** | 需真实 Hindsight/PG 才能验证 `documents.original_text IS NULL`、`llm_requests` 无副本、无 9999 listener |
| P0.5 镜像 arm64 与 digest | **OPEN** | 需 Docker/registry 访问 |
| P0.6 模型准入 | **OPEN** | 需 cliproxy 实测（本轮按计划未产生外部测试流量） |
| P0.7 测试集 | **PASS** | 32 条合成中文样本 + 20 组 query/ground truth；全部通过长度与扫描校验；三端分配 11/11/10 |
| P0.8 模板与 Compose 合同 | **PARTIAL** | 模板层 14 项通过（唯一发布端口、秘密逐项 `:?required`、`env_file` 挂接、加固、资源上限、digest pin）；`docker compose config -q` 未执行 → OPEN |
| P0.9 remote example | **PASS** | 三个 example 生成于 `templates/remote-examples/`，运行配置零引用 |
| P0.10 ingress/审计/并发/fsync | **PASS** | batch/压缩/超限/重复键/canary 全部整体拒绝；audit ingress 与终态 failpoint 行为正确；并发同 key 单 winner；全部崩溃窗口恢复无丢失/无双文件；failpoint 仅存在于测试构建 |
| P0.11 source 候选 | **N/A（本轮）** | 需在接入现场只读记录 |
| P0.12 回滚演练 | **PARTIAL** | 双清单校验层已验证（正式清单对 disposable 环境拒绝、run 级清单 dry-run/execute 通过、无 `-v`、未打标容器拒绝）；容器级 down/up 与命名卷留存未演练 → OPEN |

## 4. OPEN 项与原因

本机环境 `docker` 不可用（`command not found`），且本轮未连接 Pi5、未调用 cliproxy。以下项**必须**在具备 Docker 与真实 Hindsight 的环境重跑后才能判定：

1. **P0.3 剩余**：真实 Hindsight v0.9.0 + PostgreSQL 的 retain/recall 行为、原生 `/health`。
2. **P0.4**：`STORE_DOCUMENT_TEXT=false` 的数据库结果、`llm_requests`/audit 表无请求响应副本、OTEL exporter 缺席、`HINDSIGHT_ENABLE_CP=false` 后无 9999 listener。
3. **P0.5**：`linux/arm64` manifest 与镜像 digest 固定。
4. **P0.6**：retain 主/备模型、structured output、failover 真实接管；本地 embedding/reranker 的 ARM64 启动与中文召回。
5. **P0.8 剩余**：`docker compose config -q`、缺失变量启动失败实测、容器探针。
6. **P0.12 剩余**：disposable project 的容器级 `down/up`、命名卷数据留存。

**重要边界**：`hindsight-lab/mock_hindsight.py` 使用字符二元组词法重叠打分，**没有 embedding、reranker 或 query analyzer**。因此 `test_dataset_e2e.py` 验证的是检索**管道**（strict 标签过滤、按 `document_id` 归并、Top-5 唯一文档裁剪、卡片字段白名单、p95 延迟），**不构成 09 §10.4 的语义召回质量证据**；召回质量仍为 OPEN，须在真实 Hindsight 上测量。

## 5. 实施中发现并修复的实现缺陷

三项在编写验收测试时暴露、已修复并加了回归测试：

1. **扫描器可被中文上下文绕过**（安全相关）：`\b` 词边界在 CJK 与 ASCII 相邻处不成立，`备注AKIACANARY0EXAMPLE99` 无法命中。已将全部规则改为 ASCII 边界断言 `(?<![A-Za-z0-9])…(?![A-Za-z0-9])`，并加 `test_cjk_adjacent_secrets_still_detected` 回归。
2. **恢复语义误判**：`recovery.py` 把"staging 存在但 hash 不符"归入 `DURABILITY_GAP`；按 05 §5.3 应为 `conflict/HASH_MISMATCH`，`DURABILITY_GAP` 仅限两文件都缺失。已分离三条路径并加 `test_tampered_staging_is_conflict_not_gap`。
3. **审计故障未归一**：`failpoints.hit()` 在 `_exec` 的 try 之外抛出，注入的 SQLite 故障绕过 `AuditError` 转换、导致服务线程抛裸异常。已把注入点移入 `_exec`，与真实 SQLite 故障走同一转换路径。

另有两处按计划补强：`memory_read` 现要求 reservation provenance（无 provenance 的 Vault 文件一律 `NOT_FOUND`，符合 05 §5.3 的 `unowned_orphan` 约束）；同进程并发同 key 增加事件级互斥锁，作为 DB lease 的进程内补充。

## 6. 交付物清单

```
phase0/v4.2-final-2026-08-08/
├── scanner/                    # sp1 确定性扫描器 + canary 套件
├── hippocampus-mcp/            # MCP 服务端源码、Dockerfile（生产阶段剥离 failpoint）、测试
│   ├── src/hippocampus/        # 13 个模块
│   ├── tests/                  # 8 个验收测试模块
│   └── vendor/tiktoken-cache/  # 版本锁定的 cl100k_base 词表（离线）
├── hindsight-lab/              # 内存 mock Hindsight（仅布尔判据，无 raw capture）
├── datasets/                   # 32 条合成样本 + 20 组 ground truth（确定性生成）
├── templates/                  # compose.yaml、hindsight.env、secret plane example、回滚清单与脚本
└── reports/                    # 本报告 + 分步验收记录
```

秘密处置：本工作区**不含任何真实凭证**。测试用 Token/pepper 均为字面合成值且仅存在于测试进程内；`hippocampus.env.example` 只有 `<REDACTED>` 占位符，并由 `test_env_example_carries_no_real_values` 用扫描器守卫。

## 7. 下一步

Phase 1 起需要 Pi5 与 Docker，**不在本机可执行范围**，须按 [08-phases.md](../../plan/v4.2-final-2026-08-08/08-phases.md) Phase 1 步骤 1 先复跑 [01 §1.6](../../plan/v4.2-final-2026-08-08/01-scope-boundaries.md) 只读停止检查并取得部署授权后进行。§4 的 6 项 OPEN 应在具备 Docker 的环境优先补齐，其中 P0.4/P0.5/P0.6 是 Go/No-Go 第 1–4 条的直接前置。

---

*Phase 0 报告 · v4.2*

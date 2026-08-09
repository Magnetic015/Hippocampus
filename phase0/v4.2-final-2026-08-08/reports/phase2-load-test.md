# §10.8 / §10.4 负载与延迟报告（v4.2）

> 日期：2026-08-08 · 目标 Pi5 · 隔离 Bank `commissioning-v4-2` · commissioning 模式
> 计划：[09 §10.8 资源](../../../plan/v4.2-final-2026-08-08/09-acceptance-gonogo.md)、[09 §10.4 检索](../../../plan/v4.2-final-2026-08-08/09-acceptance-gonogo.md)、[06 §6.5](../../../plan/v4.2-final-2026-08-08/06-deployment.md)

## 1. 负载构成

从 Mac 双身份（`mac-claude` / `mac-codex` 轮换，均 peer `192.168.2.46`）驱动，目标每分钟 ≥10 search / ≥4 read / ≥2 commit(1–10 KiB) / ≥4 status。Pi5 侧每 30 s 采样内存、throttle、健康、重启计数（115 个采样点）。

实际完成：**60.5 分钟 / 47 批 / 940 请求，零错误**。

| 操作 | 次数 | 错误 |
|---|---:|---:|
| memory_search | 470 | 0 |
| memory_read | 188 | 0 |
| memory_commit（1–10 KiB） | 94 | 0 |
| memory_status | 188 | 0 |

> 说明：名义节奏为每分钟一批，但 search 延迟（见 §3）使每批实际 >60 s，故 60.5 分钟只完成 47 批，请求速率低于名义值。绝对量仍构成代表性持续负载，资源结论有效。openclaw 身份（peer `.2.3`）未纳入本负载（从 Mac 发会 source_mismatch），其接入已在 [phase2-clients.md](phase2-clients.md) 单独验证。

## 2. §10.8 资源与稳定性 — 通过

| 容器 | 峰值内存 | 硬上限 | 结果 |
|---|---:|---:|---|
| db | 115 MiB | 1024 MiB | ✅ |
| hindsight | 1670 MiB | 3072 MiB | ✅ |
| hippocampus-mcp | 94 MiB | 512 MiB | ✅ |

- **零重启**（三容器 RestartCount 全程 0）；
- **零非健康采样**（115/115 healthy）；
- 仅 `192.168.2.41:8888` 监听；现网五容器未受影响。

**部署前环境发现（非负载引入）**：`vcgencmd get_throttled` 全程恒为 `0x50000`（bit16 曾欠压 + bit18 曾降频的历史位），负载期间零变化，当前低位=0（无正在发生的降频/欠压），SoC 68.6°C 无热降频。dmesg 欠压事件早于负载启动。判定为 **Pi5 电源欠压历史**（疑似供电不足，建议核查官方 27 W USB-C PD）；与本项目负载无关，但使 §10.8 "全程无 throttling 置位" 的字面判据不满足，如实标注为环境残余项。

## 3. §10.4 检索延迟 — 不达标（记录基线）

| 操作 | p50 | p95 | max | 目标 |
|---|---:|---:|---:|---|
| search | 8955 ms | **11302 ms** | 11934 ms | **p95 ≤ 5 s** ❌ |
| read | 23 ms | 29 ms | 79 ms | — |
| commit | 43 ms | 54 ms | 72 ms | — |
| status | 19 ms | 22 ms | 28 ms | — |

read/commit/status 均在毫秒级；**唯 search 远超 5 s 目标**。

### 3.1 根因：cross-encoder reranker 的 CPU 瓶颈

Hindsight recall 服务端耗时分解（日志 `[RECALL …]`）：

```
[1] Generate query embedding:        0.09 s
[4] Reranking [cross-encoder]: 100 candidates scored in 10.4 s   ← 占 ~99%
```

`HINDSIGHT_API_RERANKER_LOCAL_MODEL=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` 对 `HINDSIGHT_API_RERANKER_MAX_CANDIDATES=100` 个候选逐个做 cross-encoder 推理，在 Pi5 ARM CPU 上单 query 8–11 s。embedding、ANN、归并均毫秒级。这是纯 CPU 推理瓶颈，非 IO/内存/配额问题。

### 3.2 可逆 A/B 实证（候选数 100 → 20）

在隔离 Bank 上做可逆 A/B（测毕已还原为 100，已核对 `RERANKER_MAX_CANDIDATES=100`）：

| RERANKER_MAX_CANDIDATES | recall 延迟 | 结果数 |
|---|---:|---:|
| 100（当前） | 10–12 s | 17–35 |
| 20 | **2.1–2.3 s** | 13–16 |

候选数与 reranker 时间近似线性；降到 20 即进入 5 s 目标内。

## 4. 处置（按 §10.4 / §6.5，不静默放行）

延迟基线已记录。固化任何检索配置改动会影响召回质量（当前中文召回质量 §10.4 本身尚未正式验收），属 [06 §6.5](../../../plan/v4.2-final-2026-08-08/06-deployment.md) 的 Phase 3 评估范畴，**需用户批准后再改**。候选方向：

1. **降 `RERANKER_MAX_CANDIDATES`（100 → 20–40）**：延迟达标，实证有效；代价是 cross-encoder 重排范围变小，排在候选 21–100 的相关结果可能漏掉——须重新验证中文召回质量。
2. **主 reranker 换 `rrf`**（已配为 `RERANKER_1`）：无神经网络、快，但为纯 rank fusion，排序质量与 cross-encoder 不同。
3. **接受当前基线**，把 search p95 阈值从 5 s 重新批准为实测值，reranker 优化列入 Phase 3。
4. **换更小/量化 reranker 或 PGroonga/VectorChord**：Phase 3 评估。

**建议**：一期先取 (1) 的折中（如 30–40，兼顾延迟与召回范围）并重验召回质量，或取 (3) 接受基线；(2)(4) 归 Phase 3。最终由用户定。

## 5. 对 Go/No-Go 的影响

- 条 5/6/7/8/10（一致性、降级、数据分层、秘密红线、审计）不受影响；
- §10.8 资源稳定性通过（除电源欠压环境项）；
- §10.4 延迟为**待决项**：须用户在"调优达标"与"重新批准阈值"之间二选一，方可推进 Go/No-Go。不阻塞功能性验收，但按计划不得静默放行。

---

*负载与延迟报告 · v4.2*

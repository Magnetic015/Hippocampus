# 02 · 最终架构（v4）

> 属于 [Hippocampus 实施计划 v4](00-overview-v4.md) · 署名：Claude · 2026-08-08

## 2.1 架构图

```mermaid
flowchart LR
    C1["本机 Claude Code"] --> M["Hippocampus MCP\n192.168.2.41:8888"]
    C2["本机 Codex"] --> M
    C3["一期一台 Hermes 代表端\n→ 192.168.2.3 OpenClaw"] --> M

    M -->|"memory_commit / memory_read"| V["MD Vault\n正文唯一事实来源"]
    M -->|"状态/任务/审计"| O["SQLite WAL\nstate + outbox + audit"]
    O --> W["单实例 index worker"]
    W -->|"仅 retrieval_text"| H["Hindsight v0.9.0\n轻索引与召回"]
    H --> P["PostgreSQL + pgvector"]
    M -->|"memory_search"| H

    R["后期：异地、Web、其他 Hermes"] -. "预留但关闭" .-> M
    B["后期：备份与 Gotify"] -. "独立评估" .-> V
```

## 2.2 责任矩阵

| 能力 | Hippocampus MCP | MD Vault | Hindsight |
|---|---|---|---|
| Agent 认证与权限 | 主责 | 无 | 仅内部 Token |
| 秘密扫描与拒绝 | 主责，最终控制 | 只接收通过内容 | 防御纵深 |
| 完整正文保存 | 编排 | 唯一事实来源 | 禁止 |
| 语义检索与排序 | 调用、过滤、裁剪 | 无 | 主责 |
| `memory://` 路径解析 | 主责 | 被受控读取 | 只保存逻辑 URI |
| 写入幂等和状态 | 主责 | 保存稳定文档 | 稳定 `document_id` upsert |
| 操作审计 | 主责（audit 表） | 无 | 无 |
| 数据重建 | 编排 | 重建源 | 可清空后重建 |

## 2.3 故障降级语义

| 故障 | `memory_commit` | `memory_read` | `memory_search` | `memory_status` |
|---|---|---|---|---|
| Hindsight/PostgreSQL 不可用 | 继续写 Vault，返回 `index_pending` | 已知安全 URI 可读 | `HIPPOCAMPUS_INDEX_UNAVAILABLE` | 可用，显示积压 |
| 秘密扫描器不可用 | fail-closed，零写入 | 仅读哈希与策略均已验证的内容；需重扫时拒绝 | 查询先做本地秘密检查，扫描器不可用时拒绝外发 | 可用 |
| SQLite/audit/registry 不可用 | 拒绝，零文件写入 | 拒绝，无法确认授权与最新安全状态 | 拒绝，无法确认授权和结果状态 | 拒绝；只保留 `/healthz`、`/readyz` |
| Vault 不可用 | 拒绝写入 | 不可读 | 可返回卡片但标明正文不可用 | 可用 |

记忆服务任何故障都不得阻塞 Agent 完成当前用户任务；Agent 应跳过记忆步骤继续当前任务。审计写入失败视同 SQLite 故障，不能出现"工具成功但无审计"的路径。

---

*署名：Claude*

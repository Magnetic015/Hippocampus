# 03 · 数据模型定稿（v4）

> 属于 [Hippocampus 实施计划 v4](00-overview-v4.md) · 署名：Claude · 2026-08-08

## 3.1 Bank 与标签

- 一期正式 Bank 固定 `main`；只存三个已授权客户端可共享的非秘密内容。
- 标签仅用于分类过滤，不构成数据库级隔离。强制标签：`src:*`、`project:*`、`scope:*`、`trust:*`、`sensitivity:*`、`type:*`。
- `src:*` 由客户端凭证映射生成；client registry 保存 `allowed_projects`、权限、允许 type 与信任上限；project/type 请求值须过服务端 allowlist，越权整笔拒绝。
- 一期服务端强制 `scope:shared`、`sensitivity:internal`；普通 Agent 写入固定 `trust:agent`，受控导入可生成 `trust:curated`，后期 Web 写入固定 `trust:unreviewed`。
- project 先规范化为 `[a-z0-9][a-z0-9_-]{0,63}` 再精确匹配；type 固定为 `decision|procedure|fact|incident|preference|constraint|reference`，不接受冒号、逗号、换行或 Unicode 混淆字符。
- **`global` project 约定（D6）**：跨项目稳定事实（用户偏好、通用约定、跨项目结论）归入保留 project `global`（Vault 路径 `shared/global/`）；三个代表客户端的 `allowed_projects` 默认包含 `global`。
- `src/trust/scope/sensitivity/tags/uri/document_id` 是保留字段，普通 Agent 一旦自报即整笔返回 `RESERVED_FIELD_FORBIDDEN`，不采用"接收后静默覆盖"。
- 一期不保存需要客户端彼此隔离的私密内容。

## 3.2 稳定 URI

```text
memory://shared/<project>/<document_id>
```

禁止在 Hindsight metadata、MCP 返回值或 Agent 配置中暴露主机绝对路径。Hippocampus 负责 URI→Vault 路径映射，拒绝 symlink、路径穿越与越界读取。

## 3.3 Markdown 结构

`retrieval_text` 必须持久化在 frontmatter（YAML 多行标量），保证不读正文即可全量重建索引：

```yaml
---
id: mem_20260808_01HXYZ
title: Pi5 Hippocampus 写入链路
summary: Hippocampus 先安全写入 Markdown，再由单实例 worker 将轻量检索文本送入 Hindsight。
retrieval_text: >-
  Pi5 192.168.2.41 上的 Hippocampus 使用 MD Vault 作为正文事实来源。
  memory_commit 完成秘密扫描和原子文件写入后返回 index_pending；单实例
  worker 只提交本字段到 Hindsight。URI 为
  memory://shared/piworkspace/mem_20260808_01HXYZ。
uri: memory://shared/piworkspace/mem_20260808_01HXYZ
project: piworkspace
source_agent: mac-codex
scope: shared
trust: agent
sensitivity: internal
type: decision
created_at: 2026-08-08T10:00:00+08:00
updated_at: 2026-08-08T10:00:00+08:00
event_at: unset
tags: [src:mac-codex, project:piworkspace, scope:shared, trust:agent, sensitivity:internal, type:decision]
---

## Detail

已移除秘密值的完整说明、必要证据与过程。（可为空，见 [07 §7.3](07-mcp-tools-audit.md)）
```

约束：

- `summary` 80–200 中文字符；`retrieval_text` 300–800 tokens，只含检索所需实体、主机、端口、错误串、事件时间、结论、约束与 URI；
- 一期每个记忆对象只对应一张索引卡；主题过多在提交前拆成多个记忆对象；
- 一期禁止 worker 将 `detail_body` 或完整 Markdown 切块送入 Hindsight（永久边界见 [05 §5.4](05-outbox-worker.md)）；
- `index_state`、`indexed_sha256`、attempt 等运行字段不写回 Markdown；`content_sha256` 对最终字节计算并只存 SQLite；
- `created_at/updated_at` 是文档生命周期时间；`event_at` 才是内容所述事件时间。调用者未提供可靠事件时间时服务端写 `unset`，不得猜测或用导入/更新时间替代。

## 3.4 Hindsight retain 载荷

```json
{
  "items": [
    {
      "document_id": "mem_20260808_01HXYZ",
      "content": "300–800 tokens 的 retrieval_text",
      "update_mode": "replace",
      "timestamp": "2026-08-08T10:00:00+08:00",
      "metadata": {
        "uri": "memory://shared/piworkspace/mem_20260808_01HXYZ",
        "project": "piworkspace",
        "source_agent": "mac-codex",
        "index_title": "Pi5 Hippocampus 写入链路",
        "index_summary": "Hippocampus 先安全写入 Markdown，再由单实例 worker 将轻量检索文本送入 Hindsight。",
        "trust": "agent",
        "sensitivity": "internal",
        "retrieval_sha256": "<retrieval_text-sha256>"
      },
      "tags": ["src:mac-codex", "project:piworkspace", "scope:shared", "trust:agent", "sensitivity:internal", "type:decision"]
    }
  ],
  "async": false
}
```

- 固定调用 `POST /v1/default/banks/main/memories`；显式 `"async": false` 并等待 Hindsight 完成，不叠加其 async operation 队列（异步边界在 Hippocampus Outbox）。
- **时间语义（D1，修订 v3）**：`timestamp` 只在 frontmatter `event_at` 为可靠 ISO 8601 时间时才出现并原样发送；`event_at: unset` 时**整个 `timestamp` 字段省略**，绝不发送 `"unset"` 等哨兵字符串——Hindsight 时间解析脆弱（#3250/#3217），非 ISO 值可能被错误解析或拒绝。Phase 0 验证两种形态。
- `index_title/index_summary` 是经扫描的索引卡字段，不是 Detail；`retrieval_sha256` 只覆盖精确 `retrieval_text`。完整 MD 的 `content_sha256` 不得进入 Hindsight。
- metadata key/value 全部为非空字符串；不允许 `null`、数组、对象、数字、布尔。

---

*署名：Claude*

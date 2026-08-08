# 03 · 数据模型定稿（v4.1）

> 属于 [Hippocampus 实施计划 v4.1](00-overview-v4.md) · 修订：Codex · 2026-08-08

## 3.1 Bank 与标签

- 一期正式 Bank 固定 `main`；Go 前保持为空。三个真实客户端的合成验收仅进入独立 Bank `commissioning-v4-1`；两 Bank 不做联合 recall/consolidation，正式模式永不查询验收 Bank。
- 标签仅用于分类过滤，不构成数据库级隔离。强制标签：`src:*`、`project:*`、`scope:*`、`trust:*`、`sensitivity:*`、`type:*`。
- `src:*` 由客户端凭证映射生成；client registry 分别保存 `readable_projects`、`writable_projects`、权限、允许 type 与信任上限；project/type 请求值须过服务端 allowlist，越权整笔拒绝。
- 一期服务端强制 `scope:shared`、`sensitivity:internal`；普通 Agent 写入固定 `trust:agent`，受控导入可生成 `trust:curated`，后期 Web 写入固定 `trust:unreviewed`。
- project 先规范化为 `[a-z0-9][a-z0-9_-]{0,63}` 再精确匹配；type 固定为 `decision|procedure|fact|incident|preference|constraint|reference`，不接受冒号、逗号、换行或 Unicode 混淆字符。
- **`global` project 约定**：跨项目稳定事实（用户偏好、通用约定、跨项目结论）可归入保留 project `global`（Vault 路径 `shared/global/`）。它不自动并入其他 project 的 search；registry 必须分别显式授予 read/write，不能因“代表客户端”身份默认获得写权限。
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
summary: Hippocampus 接收 Agent 提交后先完成授权、秘密扫描和 Markdown 原子落盘，再由单实例 Outbox worker 将已脱敏的轻量检索文本异步送入 Hindsight；完整正文始终只保留在 MD Vault。
retrieval_text: >-
  Pi5 192.168.2.41 上的 Hippocampus 使用 MD Vault 作为正文事实来源。
  memory_commit 在统一授权、JSON-RPC ingress 限型和秘密扫描通过后建立幂等
  reservation，并以相邻 staging 文件、文件和目录 fsync、atomic rename 完成
  Markdown 落盘；随后返回 index_pending。单实例 worker 只把本字段和安全
  metadata 送入 Hindsight，Detail 与完整 Markdown 永不外发。Hindsight 故障时
  正文仍可安全写入 Vault，恢复后 Outbox 自动补索引。稳定逻辑 URI 为
  memory://shared/piworkspace/mem_20260808_01HXYZ，完整文件 hash 只留 SQLite。
uri: memory://shared/piworkspace/mem_20260808_01HXYZ
project: piworkspace
source_agent: mac-codex
scope: shared
trust: agent
sensitivity: internal
type: decision
created_at: "2026-08-08T10:00:00+08:00"
updated_at: "2026-08-08T10:00:00+08:00"
event_at: unset
tags: ["src:mac-codex", "project:piworkspace", "scope:shared", "trust:agent", "sensitivity:internal", "type:decision"]
---

## Detail

已移除秘密值的完整说明、必要证据与过程。（可为空，见 [07 §7.3](07-mcp-tools-audit.md)）
```

约束：

- `summary` 60–200 中文字符；`retrieval_text` 100–600 tokens（硬上限 800），只含检索所需实体、主机、端口、错误串、事件时间、结论、约束与 URI；保持索引式摘要，不复制 Detail；
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
      "content": "100–600 tokens 的 retrieval_text（硬上限 800）",
      "update_mode": "replace",
      "timestamp": "2026-08-08T10:00:00+08:00",
      "metadata": {
        "uri": "memory://shared/piworkspace/mem_20260808_01HXYZ",
        "project": "piworkspace",
        "source_agent": "mac-codex",
        "event_at": "2026-08-08T10:00:00+08:00",
        "index_title": "Pi5 Hippocampus 写入链路",
        "index_summary": "Hippocampus 接收 Agent 提交后先完成授权、秘密扫描和 Markdown 原子落盘，再由单实例 Outbox worker 将已脱敏的轻量检索文本异步送入 Hindsight；完整正文始终只保留在 MD Vault。",
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

- 正式模式固定调用 `POST /v1/default/banks/main/memories`；commissioning mode 固定调用 `.../banks/commissioning-v4-1/memories`，Bank 均由服务端 reservation 决定，客户端不可传。显式 `"async": false` 并等待 Hindsight 完成，不叠加其 async operation 队列（异步边界在 Hippocampus Outbox）。
- **时间语义（R1/R2）**：可靠事件时间先规范化为 RFC 3339，再同时写顶层 `timestamp` 与 metadata `event_at`；无可靠时间时两者都使用字符串 `"unset"`。Hindsight v0.9.0 省略 timestamp 会默认当前 UTC，只有显式 `"unset"` 才生成无时间事实，因此禁止省略、传 `null` 或用导入/更新时间替代。Phase 0 必须验证两种形态及数据库结果。
- `index_title/index_summary` 是经扫描的索引卡字段，不是 Detail；`retrieval_sha256` 只覆盖精确 `retrieval_text`。完整 MD 的 `content_sha256` 不得进入 Hindsight。
- metadata key/value 全部为非空字符串；不允许 `null`、数组、对象、数字、布尔。

---

*修订：Codex*

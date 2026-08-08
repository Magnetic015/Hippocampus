# Phase 2 客户端接入报告（v4.2）

> 日期：2026-08-08 · 三代表客户端：本机 Claude Code、本机 Codex、`192.168.2.3` OpenClaw  
> 计划：[08 Phase 2](../../../plan/v4.2-final-2026-08-08/08-phases.md)、验收 [09 §10.6](../../../plan/v4.2-final-2026-08-08/09-acceptance-gonogo.md)

## 1. 结论

三端 MCP 配置均已就位并各自以独立身份接入。OpenClaw 端在其真实常驻运行时完成了 `initialize → tools/list` 与能力探测，两项热加载（配置、Token）实测不重启进程。本机两端配置写入完成，但其真实运行时 E2E 必须由用户在各自客户端内完成（见 §5）。

变更前均建带时间戳 0600 备份，配置只引用环境变量 / SecretRef，无明文 Token 落入项目或 shell profile。

## 2. 能力预检（只读）

| 客户端 | 原生 Streamable HTTP MCP | Bearer 安全引用 | 配置热加载 | Token 轮换热加载 | 判定 |
|---|---|---|---|---|---|
| Claude Code | `type:http` + headers | `${HIPPOCAMPUS_CLAUDE_TOKEN}` 环境展开 | — | — | 支持 |
| Codex | 原生 Streamable HTTP | `bearer_token_env_var` | — | — | 支持 |
| OpenClaw 2026.6.8 | `mcp add --transport streamable-http` | `--header` + SecretRef | `mcp reload`（不重启） | `secrets reload`（原子快照替换，不重启） | 支持 |

OpenClaw 的停止条件（"任一变更必须重启才生效"）**不触发**：`mcp reload` 与 `secrets reload` 前后 gateway PID 均为 `319099` 未变。

## 3. `.2.3` OpenClaw 接入路径

初次 SSH 以 `kkp`/`magnetic` 等被 publickey 拒绝；实际可用账户为 `debian@192.168.2.3`（主机名 `dockerNode`，与 client_id `dockerNode-openclaw` 吻合）。

OpenClaw 形态：npm 全局安装、`node …/openclaw/dist/index.js gateway --port 18789` 常驻进程（非容器，已运行数日），配置在 `/home/debian/.openclaw/openclaw.json`。不装 Hindsight 自动 retain 插件、不直连 Hindsight、未改其他服务。

配置命令（`mcp add` 先探测再保存）：

```
openclaw mcp add hippocampus \
  --transport streamable-http --url http://192.168.2.41:8888/mcp/ \
  --header "Authorization=Bearer <token>" \
  --include "memory_search,memory_read,memory_commit,memory_status" \
  --timeout 180 --connect-timeout 20
→ Saved MCP server "hippocampus"
openclaw mcp probe hippocampus → hippocampus: 4 tools
```

Token 由 Pi5 经 SSH 管道直传到 `.2.3` 的 `/home/debian/.openclaw/secrets/hippocampus.token`（0600），未经过操作者上下文。

## 4. 服务端侧核对（Pi5 审计）

- 三个独立身份均出现在 audit：`mac-claude`、`mac-codex`、`dockerNode-openclaw`；
- OpenClaw 请求 `client_id=dockerNode-openclaw`、`initialize/tools_list` 全部 `ok`，已认证行 `source_ip=None`；
- peer 绑定精确命中：来自 `192.168.2.3` 的 `source_denied` / `source_mismatch` 计数为 **0**；
- registry：`dockerNode-openclaw` 绑定 `192.168.2.3`、仅授 `commissioning` 读写、14 天到期；`mac-claude`/`mac-codex` 绑定 `192.168.2.46`（共享 Mac peer，Token 分离）。

## 5. 必须由用户完成的部分

本机 Claude Code 与 Codex 的 CLI 不在本自动化 shell 的 PATH 中（它们运行在 GUI / 其他运行时）。计划的真实 commissioning E2E 明确要求"在各自真实运行上下文"执行且"curl/SDK 不能替代"，因此这两端的 E2E **只能由用户在真实客户端内完成**：

1. 在 Claude Code 运行环境导出 `HIPPOCAMPUS_CLAUDE_TOKEN`（值在 Pi5 `/home/kkp/.config/hippocampus/tokens/mac-claude.token`），重启客户端使 MCP 生效；
2. 在 Codex 宿主进程可见处导出 `HIPPOCAMPUS_CODEX_TOKEN`（Pi5 `…/mac-codex.token`），GUI 不继承 shell 环境时用 OS 凭证存储；
3. 三端各自依次 `memory_commit → memory_status → memory_search → memory_read`，并经 `memory_commit` 导入各自分配子集（[phase1-assignment-manifest.json](../datasets/phase1-assignment-manifest.json)，合计 32 条），只用 `project=commissioning`。

Token 值我不写入你的 shell profile；上面路径需你自行取用。

## 6. 仍为 OPEN（Phase 2 剩余 + 收口）

| 项 | 状态 |
|---|---|
| 本机 Claude / Codex 真实运行时 E2E | 待用户执行（§5） |
| 三端合成子集导入（32 条）与 §10.4 中文召回质量 | 待 E2E 后 |
| §10.8 60 分钟混合负载 | 未执行 |
| §10.9 commissioning 收口（撤销/轮换 PoC Token、移除 commissioning 权限、切 `main`、按 manifest 显式授权实际 project） | 未执行 |
| Go/No-Go 条 12 内部 TLS 或 LAN 明文残余风险决定 | 未决 |

**Go / No-Go：尚未评估。** 正式 `main` 保持为空；全部数据在隔离 Bank。

## 7. 客户端备份与回滚

| client | backup_id | 路径 | 权限 | integrity_verified |
|---|---|---|---|---|
| mac-claude | BK-fc60e4a8 | `~/.hippocampus-client-backups/mac-claude.20260808T162150.*` | 0600 | true |
| mac-codex | BK-e4d6ba3a | `~/.hippocampus-client-backups/mac-codex.20260808T162150.*` | 0600 | true |

备份本体、回滚记录（`restore-record.tsv`）留在 `~/.hippocampus-client-backups/`（0700，非 Git）。OpenClaw 侧改动为新增一个 MCP server 条目，卸载即 `openclaw mcp unset hippocampus`。回滚脚本 `rollback-clients.sh` 可对上述记录执行可逆恢复，输出仅安全元数据。

---

*Phase 2 客户端接入报告 · v4.2*

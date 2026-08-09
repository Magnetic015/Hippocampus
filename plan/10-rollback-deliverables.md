# 10 · 回滚原则、实施交付物与参考资料（v4）

> 属于 [Hippocampus 实施计划 v4](00-overview-v4.md) · 署名：Claude · 2026-08-08
> （D4：本文件补全 v3 在 §13 中途截断缺失的交付物与参考资料）

## 12. 回滚原则

- 变更限定 [01 §1.5](01-scope-boundaries.md) 允许面；
- 客户端配置修改前建带时间戳备份；
- 回滚先撤销三客户端 Token，再恢复客户端配置；
- 停止/移除只针对 Hippocampus 专属 Compose project 和精确命名的本机 Phase 0 disposable project；
- 默认保留 Vault、state（含审计）与数据库 volume，删除须另行授权；
- 不删除用户文件、不运行 Docker prune、不新建/删除主机账户、不触碰 OpenClaw adapter（一期未安装）、不重启现有业务；
- 回滚后验证 8888 监听消失、客户端不再连接、现有服务健康，Gotify/Backup/Gateway 仍无变化。

## 13. 实施交付物

### 13.1 Pi5

```text
/home/kkp/hippocampus/
├── compose.yaml
├── hindsight.env               # 仅非秘密固定项
├── hindsight.env.example
├── hippocampus-mcp/            # 网关源码 + Dockerfile（固定 commit）
├── vault/
│   ├── shared/
│   │   ├── global/             # D6：跨项目通用记忆
│   │   ├── infra/
│   │   └── projects/
│   └── inbox/
├── state/                      # 0700
│   ├── outbox.db               # state+registry+outbox+audit，0600（含 WAL/SHM）
│   └── audit-export/           # 0700，JSONL 0600
├── scripts/
│   ├── preflight-readonly.sh
│   ├── verify-compose-secret-mapping.sh
│   ├── verify-data-minimization.sh
│   ├── verify-secret-redline.sh
│   ├── verify-authorization.sh
│   ├── verify-event-time.sh
│   ├── verify-hindsight-no-trace.sh
│   ├── verify-outbox-failpoints.sh
│   ├── verify-audit-failpoints.sh
│   ├── export-audit.sh
│   ├── rebuild-disposable-bank.sh
│   └── rollback-clients.sh
├── remote-examples/            # 生成但不加载
│   ├── compose.remote.yaml.example
│   ├── Caddyfile.example
│   └── oauth-boundary.md
└── backups-test/               # 仅合成数据的一次性人工导出/恢复测试

/home/kkp/.config/hippocampus/  # 运行 secret plane，0700/0600
├── hippocampus.env             # --env-file 插值源
└── （客户端 Token 一次性交付文件，确认后删除）
```

### 13.2 本机（Mac）

```text
/Users/magnetic/hippocampus/
├── plan/                       # 本 v4 计划文档集（00–10）
└── phase0/                     # Phase 0 隔离验证工作区
    ├── scanner/                # 秘密扫描器 + canary 测试
    ├── hindsight-lab/          # disposable compose（前缀 hippocampus-phase0-）
    ├── datasets/               # 30–50 条中文样本 + query/ground-truth
    ├── reports/                # Phase 0 验证报告、digest 清单
    └── templates/              # compose/env/secret plane/remote example 模板
```

### 13.3 附加交付物

- 三客户端配置备份与回滚记录；
- 镜像 tag/digest/源码 commit 清单；
- 固定测试集、ground truth 与验收报告（对照 [09](09-acceptance-gonogo.md) 全清单）；
- 端口、容器、网络与现网未扰动对比记录；
- 审计导出样例（已脱敏）；
- 所有真实秘密均已从报告、命令记录与日志排除的证明。

## 14. 参考资料

- [Hindsight GitHub](https://github.com/vectorize-io/hindsight) · [v0.9.0 Release](https://github.com/vectorize-io/hindsight/releases/tag/v0.9.0)
- [Configuration](https://hindsight.vectorize.io/developer/configuration) · [Retain API](https://hindsight.vectorize.io/developer/api/retain) · [MCP Server](https://hindsight.vectorize.io/developer/mcp-server) · [Installation](https://hindsight.vectorize.io/developer/installation)
- Issues：[#3227 同 Bank 并发 retain 死锁](https://github.com/vectorize-io/hindsight/issues/3227) · [#3209 metadata null](https://github.com/vectorize-io/hindsight/issues/3209) · [#3194 consolidation 卡死](https://github.com/vectorize-io/hindsight/issues/3194) · [#3250 时间解析](https://github.com/vectorize-io/hindsight/issues/3250) · [#3217 极端日期崩溃](https://github.com/vectorize-io/hindsight/issues/3217) · [#3123/#3114/#3115 自动 retain 插件缺陷](https://github.com/vectorize-io/hindsight/issues/3123)
- v0.9.0 源码复核：`hindsight-api-slim/hindsight_api/mcp_tools.py`（recall 简单 tags 过滤含未打标条目、`tag_groups` strict）、`config.py`（LLM 编号 failover 成员不继承 key）、`engine/response_models.py`（MemoryFact metadata/tags 回传）
- [Claude Code MCP](https://code.claude.com/docs/en/mcp) · [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp)
- 历史版本：[调研报告](/Users/magnetic/claudeWorkspace/hippocampus-pi5-research-2026-08-08.md) · [综合定稿](/Users/magnetic/claudeWorkspace/hippocampus-pi5-combined-evaluation-and-implementation-plan-2026-08-08.md) · [final v1](/Users/magnetic/claudeWorkspace/hippocampus-pi5-final-revised-implementation-plan-2026-08-08.md) · [v2](/Users/magnetic/claudeWorkspace/hippocampus-pi5-final-implementation-plan-v2-2026-08-08.md) · [v3](/Users/magnetic/claudeWorkspace/hippocampus-pi5-final-implementation-plan-v3-2026-08-08.md)

---

*署名：Claude*

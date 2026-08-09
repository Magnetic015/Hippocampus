# 10 · 回滚原则、实施交付物与参考资料（v4.2）

> 属于 [Hippocampus 实施计划 v4.2](00-overview-v4.md) · final · 2026-08-08

## 12. 回滚原则

- 变更限定 [01 §1.5](01-scope-boundaries.md) 允许面；
- 客户端配置修改前建带时间戳备份；
- 回滚先用非 MCP registry CLI 按 client_id 撤销三客户端 Token，再恢复客户端配置；撤销不需要也不得读取旧明文 Token。若 registry/SQLite 不可用，立即用精确 project 命令停止 `hippocampus-mcp`、确认 8888 消失，再恢复客户端，随后继续服务端 down；不得为等待撤销而保持 ingress；
- 服务端回滚只认**环境回滚清单**（F2）：非秘密文件，固定目标 Compose project、期望标签与 compose 路径。正式清单 `rollback-manifest-prod.env` 固定 `project=hippocampus`、`io.hippocampus.managed=true`、`io.hippocampus.plan=v4.2`；Phase 0 每个 run 生成 disposable 清单（`project=hippocampus-v4-2-phase0-<run_id>`、`io.hippocampus.plan=v4.2-phase0`）。`rollback-server.sh` 核对清单与实际对象的 project 与双标签，任何不匹配一律拒绝，然后执行不带 `-v` 的精确 Compose down；本机只清理 `hippocampus-v4-2-phase0-<run_id>` disposable project；
- 默认保留 Vault、state（含审计）与数据库 volume，删除须另行授权；
- 不删除用户文件、不运行 Docker prune、不新建/删除主机账户、不触碰 OpenClaw adapter（一期未安装）、不重启现有业务；
- 回滚后验证 8888 监听消失、客户端不再连接、现有服务健康，Gotify/Backup 未被接入。Gateway 未参与只依据 Hippocampus 变更清单、命令记录、配置引用扫描与测试流量目标证明；不得为部署、验收或回滚探测其配置、端口、进程或健康状态。
- 客户端备份本体与完整性摘要始终留在各自原安全域或独立 0700/0600 非 Git 本地 manifest；恢复时在该安全域校验，项目报告不保存内容/diff/hash/HMAC/大小，只记录随机安全备份 ID、路径、时间、权限、`integrity_verified` 布尔值与回滚结果。

## 13. 实施交付物

### 13.1 Pi5

```text
/home/kkp/hippocampus/
├── compose.yaml
├── hindsight.env               # 仅非秘密固定项
├── hindsight.env.example
├── hippocampus-mcp/            # 网关源码 + Dockerfile（固定 commit）
├── vault/
│   └── shared/                 # 统一映射：memory://shared/<project>/<id> ↔ shared/<project>/<id>.md
│       ├── global/             # 保留 project：跨项目通用记忆
│       ├── commissioning/      # 隔离验收数据（默认保留，不参与正式检索）
│       └── <project>/          # 每个业务 project 一个目录，无其他特例路径
├── state/                      # 0700
│   ├── outbox.db               # registry+reservation+outbox+audit，0600（含 WAL/SHM）
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
│   ├── verify-jsonrpc-ingress.sh
│   ├── verify-idempotency-concurrency.sh
│   ├── verify-docker-boundary.sh
│   ├── export-audit.sh
│   ├── registry-bootstrap.sh
│   ├── registry-issue.sh
│   ├── registry-rotate.sh
│   ├── registry-revoke.sh
│   ├── registry-bind-peer.sh
│   ├── registry-revoke-peer.sh
│   ├── registry-list-safe.sh
│   ├── rebuild-disposable-bank.sh
│   ├── rollback-manifest-prod.env  # 正式环境回滚清单（非秘密）
│   ├── rollback-clients.sh
│   └── rollback-server.sh      # 按环境回滚清单核对 project/labels，禁止 down -v
├── remote-examples/            # 生成但不加载
│   ├── compose.remote.yaml.example
│   ├── Caddyfile.example
│   └── oauth-boundary.md
└── backups-test/               # 仅合成数据的一次性人工导出/恢复测试
```

`/home/kkp/.config/hippocampus/`（运行 secret plane，0700/0600）：`hippocampus.env`（`--env-file` 插值源）与客户端 Token 一次性交付文件（确认后删除）。

### 13.2 本机（Mac）

```text
/Users/magnetic/hippocampus/
├── plan/
│   └── v4.2-final-2026-08-08/    # 本独立实施版本集（00–11 + MANIFEST.sha256）
└── phase0/
    └── v4.2-final-2026-08-08/    # 仅本版 Phase 0 隔离验证工作区
        ├── scanner/                # 秘密扫描器（sp1）+ canary 测试
        ├── hindsight-lab/          # disposable compose（前缀 hippocampus-v4-2-phase0-）
        ├── datasets/               # 30–50 条中文样本 + query/ground-truth
        ├── reports/                # Phase 0 验证报告、digest 清单、分步验收证据
        └── templates/              # compose/env/secret plane/回滚清单/remote example 模板
```

### 13.3 附加交付物

- 三客户端配置备份与回滚记录；
- 三客户端配置备份仅交付 `client_id, 随机安全备份 ID, 安全绝对路径, 时间, 权限, integrity_verified, 回滚结果` 元数据；备份正文、diff、hash/HMAC、大小和其他指纹不进入项目；
- 镜像 tag/digest/源码 commit 清单；
- 固定测试集、ground truth 与验收报告（对照 [09](09-acceptance-gonogo.md) 全清单）；
- **分步验收记录**：对照 [11-step-acceptance.md](11-step-acceptance.md) 逐步登记判定结果与证据编号；
- 端口、容器、网络与现网未扰动对比记录；
- 审计导出样例（已脱敏）；
- 所有真实秘密均已从报告、命令记录与日志排除的证明。
- 固定 Compose inventory（project/service/network/volume/mount/labels/digest）、Docker 管理面受信主体清单、source allowlist 决策记录；
- commissioning manifest、三真实客户端 E2E 记录、PoC Token 撤销/轮换与 commissioning 权限移除证明；
- 本版 `MANIFEST.sha256` 及所有 Phase 0 模板/报告绑定的 plan manifest digest；

## 14. 参考资料

- [Hindsight GitHub](https://github.com/vectorize-io/hindsight) · [v0.9.0 Release](https://github.com/vectorize-io/hindsight/releases/tag/v0.9.0)
- [Configuration](https://hindsight.vectorize.io/developer/configuration) · [Retain API](https://hindsight.vectorize.io/developer/api/retain) · [MCP Server](https://hindsight.vectorize.io/developer/mcp-server) · [Installation](https://hindsight.vectorize.io/developer/installation)
- Issues：[#3227 同 Bank 并发 retain 死锁](https://github.com/vectorize-io/hindsight/issues/3227) · [#3209 metadata null](https://github.com/vectorize-io/hindsight/issues/3209) · [#3194 consolidation 卡死](https://github.com/vectorize-io/hindsight/issues/3194) · [#3250 时间解析](https://github.com/vectorize-io/hindsight/issues/3250) · [#3217 极端日期崩溃](https://github.com/vectorize-io/hindsight/issues/3217) · [#3123/#3114/#3115 自动 retain 插件缺陷](https://github.com/vectorize-io/hindsight/issues/3123)
- v0.9.0 源码复核：`hindsight-api-slim/hindsight_api/mcp_tools.py`（recall 简单 tags 过滤含未打标条目、`tag_groups` strict）、`config.py`（LLM 编号 failover 成员不继承 key）、`engine/response_models.py`（MemoryFact metadata/tags 回传）
- v0.9.0 时间语义：[HTTP schema](https://github.com/vectorize-io/hindsight/blob/v0.9.0/hindsight-api-slim/hindsight_api/api/http.py) 明确 `unset`，且 [retain orchestrator](https://github.com/vectorize-io/hindsight/blob/v0.9.0/hindsight-api-slim/hindsight_api/engine/retain/orchestrator.py) 对省略 timestamp 默认当前时间；standalone `start-all.sh` 使用 `HINDSIGHT_ENABLE_CP=false` 禁用 Control Plane；
- [Claude Code MCP](https://code.claude.com/docs/en/mcp) · [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp)
- 历史版本：[调研报告](/Users/magnetic/claudeWorkspace/hippocampus-pi5-research-2026-08-08.md) · [综合定稿](/Users/magnetic/claudeWorkspace/hippocampus-pi5-combined-evaluation-and-implementation-plan-2026-08-08.md) · [final v1](/Users/magnetic/claudeWorkspace/hippocampus-pi5-final-revised-implementation-plan-2026-08-08.md) · [v2](/Users/magnetic/claudeWorkspace/hippocampus-pi5-final-implementation-plan-v2-2026-08-08.md) · [v3](/Users/magnetic/claudeWorkspace/hippocampus-pi5-final-implementation-plan-v3-2026-08-08.md) · v4（`plan/` 根） · v4.1（`plan/v4.1-revised-2026-08-08/`）

---

*v4.2 final*

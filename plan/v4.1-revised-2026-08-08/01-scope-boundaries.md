# 01 · 统一口径与硬边界（v4.1）

> 属于 [Hippocampus 实施计划 v4.1](00-overview-v4.md) · 修订：Codex · 2026-08-08

## 1.1 组件定义

- **记忆中枢（Hippocampus）**：完整系统名称。
- **Hippocampus MCP**：所有 Agent 的唯一记忆入口和控制面。
- **Hindsight**：内部可重建的智能索引与检索引擎。
- **MD Vault**：完整正文的唯一事实来源。
- **SQLite state/reservation/outbox/audit**：幂等 reservation、写入状态、索引任务指针与操作审计；不是正文仓库。
- **PostgreSQL/pgvector**：Hindsight 的内部持久化层；不是正文事实来源。

明确结论：MCP 写入和读取**不是**由 Hindsight 完全控制。`memory_commit` 由 Hippocampus 完成授权、秘密扫描、MD 落盘与 Outbox；`memory_read` 由 Hippocampus 直接读 Vault；只有 `memory_search` 的智能召回、排序和索引派生主要由 Hindsight 提供。Hindsight 可全部重建和替换，不是事实源或 Agent 权限控制面。

## 1.2 永久秘密红线

> Hippocampus 不是秘密管理器。任何 Agent、管理员工具、导入脚本、worker、重试、reconcile 或后期客户端均不得把秘密材料写入 Bank、Vault、Hindsight、PostgreSQL、SQLite、Outbox、审计、死信、日志、trace 或备份。

禁止内容至少包括：密码/PIN/恢复码/助记词/TOTP seed；API key、Bearer/Access/Refresh/ID Token、JWT、OAuth client secret；Session Cookie、认证 Header、webhook/signing secret；SSH/PGP/TLS 私钥、云平台凭证、带凭证 kubeconfig；含明文凭证的 URL/DSN/`.env`/命令/配置/日志；上述内容的 Base64、URL 编码、分片、转义或混淆形式；可用于身份冒用、账号接管或资金操作的完整高风险身份与支付材料。

允许内容仅限：`${TOKEN}`、`<REDACTED>` 等无实际值占位符；外部秘密管理器逻辑条目名；轮换日期、非认证用途指纹及不含实际秘密的操作说明。

用户指令、管理员身份、`internal/private/restricted` 标签、私有 Bank、加密备份、编码分片或 `force/allow_secret` 参数均不构成豁免。

## 1.3 一期明确不做

一期统一指 Phase 0–2，明确排除：

- Gotify 通知接入（不创建 app/token/webhook、不发送通知）；
- Pi5 备份枢纽（不调 Backup API、不注册任务、不改 `backup-hub-maintain.timer`、不以备份目的写/读/挂载 `/srv/backup`）；
- 异地、在线 Web 与除 `.2.3` 代表端之外的其他 Hermes 接入；
- 除 `192.168.2.41:8888 → 8080` 外的任何主机端口映射，尤其是 WAN/Public 映射；
- 公网 DNS/证书/Caddy/防火墙/路由修改；
- 4 GB NVMe swapfile；
- auto-consolidation、Knowledge Pages 与普通 Agent 管理工具；
- Hindsight Control Plane：一期固定 `HINDSIGHT_ENABLE_CP=false`，容器内外均不得监听或访问 9999；
- 将 `.2.41` Claude CLI Gateway 用作任何验证端点。

Gotify 与备份枢纽仅保留后期独立评估入口，评估通过不等于授权实施。

## 1.4 `.2.41` Claude CLI Gateway 排除范围

一期不得探测、调用、读取或修改其配置；不得重启或停止其服务；不得复用其端口、Token、环境变量、会话或模型路由；不得写入 Compose、client registry、测试脚本或验收端点。模型验证只针对用户指定的 cliproxy 接口。

## 1.5 一期允许变更面

仅允许：

- 本机 `/Users/magnetic/hippocampus/` 项目根：本 v4.1 计划目录、版本化 `/Users/magnetic/hippocampus/phase0/v4.1-revised-2026-08-08/` 的 Phase 0 源码/配置/测试结果，以及命名为 `hippocampus-v4-1-phase0-<run_id>` 的 disposable Compose 对象；
- Pi5 `/home/kkp/hippocampus/`、`/home/kkp/.config/hippocampus/`；
- Hippocampus 专属 Compose project、三个容器、内部 network/volumes、正式 Hindsight Bank `main`、隔离验收 Bank `commissioning-v4-1` 和唯一 LAN 映射 `192.168.2.41:8888 → 8080`；
- 三个代表客户端各自最小 MCP 配置及其带时间戳安全备份。

Go 前合成数据只进入隔离 Bank `commissioning-v4-1` 的 project `commissioning`，不得进入正式 `main`；Agent 不能传 Bank，服务端 commissioning mode 强制路由。验收后必须移除普通客户端权限、轮换三端 Token 并切换正式模式到 `main`。隔离 Bank/对应 Vault 测试数据默认保留作不可见回归基线，删除需另行授权，且不会参与正式检索或 Hindsight 跨项目归并。

Pi5 使用现有 `kkp` 账户管理目录，容器内用固定非 root UID/GID；一期**不新建主机账户**，不修改 `/etc/passwd` 或 `/etc/group`。其他现网对象默认不可修改。

## 1.6 部署前停止条件

只读预检发现任一情况必须停止：

- `/home/kkp/hippocampus/` 已存在未知内容；
- 容器名/Compose project/network/volume 冲突；
- `192.168.2.41` 不在目标接口；`8888` 已被占用；
- 镜像无 arm64 manifest；
- NVMe 空间或可用内存明显低于已评估基线；
- Docker/Compose 解析失败；
- 本版 `MANIFEST.sha256` 校验失败，版本化 Phase 0 根含来源 digest 不符或未知产物；
- secret plane 权限不满足 `0700/0600`；
- Docker socket owner/group/ACL、`docker` group、当前 Docker context、`DOCKER_HOST`、TCP daemon 或其他管理 API 含未批准管理者/远程端点；Docker 管理权限按等价 root 处理；
- Compose 渲染结果出现 `privileged`、host network/PID/IPC、Docker socket/管理 API、非必要 device/capability、未声明 mount，或 project/network/volume 名称不符合 [06](06-deployment.md) 固定 inventory；
- 对即将启用的客户端没有非空精确 source allowlist，或候选只能写成网段、通配、`0.0.0.0/0`/`::/0`；
- Hindsight Control Plane 无法完全禁用，或容器内出现 9999 listener；
- 需要新主机账户、覆盖、prune、重启或删除现有业务对象才能继续；
- OpenClaw 原生能力不足而需要 adapter/skill，或最小 MCP 配置、Bearer 引用、PoC→正式 Token 轮换任一项必须通过重启 `.2.3` OpenClaw 才能生效（均走独立子计划与新授权）。

禁止 `docker system prune`；禁止覆盖未知目录；禁止重启 Home Assistant、Gotify、Backup API、Caddy、`.2.3` OpenClaw 或其他现有容器。Phase 2 先只读确认 OpenClaw 对配置与凭证轮换均能热加载；两者都支持才执行可回滚的最小 MCP reload，任一必须重启则停在能力报告。需要 adapter/skill 时只生成明确的变更子计划（版本、路径、服务、网络、权限、卸载回滚），取得新授权后再执行。

---

*修订：Codex*

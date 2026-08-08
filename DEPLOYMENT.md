# Hippocampus 部署手册（Deployment Runbook）

> 面向**后期迭代部署与日常运维**。基于 `plan/v4.2-final-2026-08-08/` 设计 + Pi5 现网实况（截至 2026-08-08）。
> 全文**不含任何密钥值**，密钥一律以变量名/位置引用。设计与现网有出入处，以「现网实况」为准并标注。
> 深度背景见 `plan/v4.2-final-2026-08-08/00…11-*.md` 与 `phase0/v4.2-final-2026-08-08/reports/*.md`。

## 目录
0. [TL;DR 速查](#0-tldr-速查) · 1. [系统概览](#1-系统概览) · 2. [架构与拓扑](#2-架构与拓扑) · 3. [主机前置条件](#3-主机前置条件) · 4. [文件与目录地图](#4-文件与目录地图host) · 5. [配置参考](#5-配置参考) · 6. [首次部署](#6-首次部署phase-1-概要) · 7. [迭代部署与升级](#7-迭代部署与升级) · 8. [客户端接入](#8-客户端接入) · 9. [MCP 工具与调用约定](#9-mcp-工具与调用约定) · 10. [运维](#10-运维) · 11. [回滚](#11-回滚) · 12. [故障排查（实战）](#12-故障排查实战) · 13. [已知问题与待决策](#13-已知问题与待决策) · 14. [当前状态快照](#14-当前状态快照-2026-08-08) · [附录](#附录-a关键命令速查)

---

## 0. TL;DR 速查

- **现网**：Raspberry Pi 5 @ `kkp@192.168.2.41`,3 个容器,Compose project = `hippocampus`,**仅** `192.168.2.41:8888` 对外(MCP 唯一入口)。
- **迭代 MCP 镜像**：改宿主源码 → `docker build` → 改 env 里 `HIPPOCAMPUS_MCP_TAG` → `docker compose … up -d hippocampus-mcp` → 验健康 + A/B。
- **回滚服务**：`scripts/rollback-server.sh --manifest scripts/rollback-manifest-prod.env --execute`(**永不 `-v`**,卷与数据始终保留)。
- **客户端下发**:`hippocampus-registry issue/grant/bind-peer/rotate/revoke`(token 只走 stdin/0600 文件)。
- **铁律**:agent 只经 MCP 访问,禁止直连 Hindsight;先过安全控制再落库;密钥零持久化;开 8888 之前必须先装好非空来源白名单 + peer 绑定。

---

## 1. 系统概览

自托管的 **MCP 记忆中枢**:给 AI agent 提供持久、可检索的记忆。Pi5 上以 Docker Compose 运行,索引引擎为 Hindsight `v0.9.0`。

**核心不变量**(`00-overview-v4.md`):Hindsight 只存**可重建的轻量索引**(`retrieval_text` + 安全元数据),正文只在 **MD Vault**;所有 agent **只经 Hippocampus MCP** 访问;写主链同步落盘、旁链经 Outbox 异步索引;密钥在每条路径 fail-closed / 零持久化;每个已认证 MCP 请求产生**恰好一条**脱敏审计行,正常成功响应必须在其终态审计提交后才返回。

**组件角色**:
| 组件 | 角色 | 真值? |
|---|---|---|
| Hippocampus MCP | 唯一 agent 入口 + 控制面(认证/密钥扫描/写 MD/Outbox) | — |
| Hindsight | 内部可重建语义索引/检索,**非**真值、**非**授权面 | 否 |
| MD Vault | 全文正文的**唯一真值** | 是 |
| SQLite（state） | 幂等预留 + 写状态 + Outbox 指针 + 审计(非正文库) | — |
| PostgreSQL/pgvector | Hindsight 内部持久化 | 否 |

---

## 2. 架构与拓扑

3 容器,Compose project **`hippocampus`**,内部网络 `hippocampus-backend`(bridge,`internal:false` 以便 Hindsight 出网调用 cliproxy/HF)。

| 服务 | 容器名 | 镜像引用(现网) | 端口 | 卷 | 内存上限 |
|---|---|---|---|---|---|
| db | `hippocampus-db-1` | `pgvector/pgvector@sha256:${PGVECTOR_DIGEST}` | 不发布 | `hippocampus-pgdata` | 1 GiB |
| hindsight | `hippocampus-hindsight-1` | `ghcr.io/vectorize-io/hindsight@sha256:${HINDSIGHT_DIGEST}` | 不发布 | `hippocampus-hf-cache` | 3 GiB |
| hippocampus-mcp | `hippocampus-hippocampus-mcp-1` | `hippocampus-mcp:${HIPPOCAMPUS_MCP_TAG}`(**本地构建、tag 引用**) | `192.168.2.41:8888→8080` | bind `./vault→/data/vault`、`./state→/data/state` | 512 MiB |

- **对外仅 8888**;Hindsight 原生 API/MCP、db 5432、控制面 9999 **均不发布**(`HINDSIGHT_ENABLE_CP=false`)。
- **MCP 路由**:`/healthz`(存活)、`/readyz`(就绪)、`/version`、`/mcp/`(agent 入口)。
- **数据流**:`commit`/`read` ↔ Vault(正文真值);预留/状态/审计 → SQLite WAL → 单实例 worker → 仅 `retrieval_text`+安全元数据 → Hindsight → PG/pgvector;`search` → Hindsight 召回 → MCP 内做**服务端二次授权/合并/输出扫描**。
- **降级矩阵**:Hindsight/PG 挂 → commit 仍写 Vault(`index_pending`)、已知 URI 可 read、search 返回 `HIPPOCAMPUS_INDEX_UNAVAILABLE`;Scanner/SQLite/审计挂 → 四个数据工具**全部 fail-closed**,仅健康路由存活;Vault 挂 → 拒绝 commit。MCP **不得**硬依赖 Hindsight 健康。
- **重建边界(P1 残留风险)**:Phase 1 无 `memory_reindex`/`reconcile`(Phase 3 才有)。若 Hindsight/PG 数据丢失,Vault 正文 + SQLite 状态仍在、commit/read/status 可用,但批量重建需 Phase 3 授权,期间 search 降级。

---

## 3. 主机前置条件

- Pi5(**arm64**),Docker 29.6.1 / Compose v5.3.1(现网),无 swapfile。
- `kkp` 在 `docker` 组(故 `docker`/`docker compose` 免 sudo)。⚠️ `docker` 组成员 = `kkp, beszel`;`beszel` 监控 agent 视同 root、可读全部注入密钥,**已被记录接受**(见 §13)。
- 目录/权限:密钥面 `/home/kkp/.config/hippocampus/`(目录 0700 / 文件 0600 / umask 077);state、vault bind 0700/0600。**不新建主机账号**。
- 三容器**不得**与 Home Assistant / Gotify / Backup API / Caddy 共享 project 或网络。

---

## 4. 文件与目录地图（host）

**Pi5(`kkp@192.168.2.41`)**
| 路径 | 内容 | 入库? |
|---|---|---|
| `/home/kkp/hippocampus/compose.yaml` | 三服务定义(digest/TAG、卷、硬化、限额) | 模板在仓库 |
| `/home/kkp/hippocampus/hindsight.env` | Hindsight **非密钥**配置,作为 `env_file` 挂载 | 模板在仓库 |
| `/home/kkp/hippocampus/hippocampus-mcp/` | MCP 源码 + Dockerfile(构建上下文) | 在仓库 |
| `/home/kkp/hippocampus/scripts/` | `rollback-server.sh`、`rollback-clients.sh`、`rollback-manifest-prod.env` | 在仓库 |
| `/home/kkp/hippocampus/vault/shared/<project>/<id>.md` | 正文真值(bind 到 MCP) | **否**(运行时数据) |
| `/home/kkp/hippocampus/state/outbox.db` | SQLite:注册表/幂等/Outbox/审计 | **否** |
| `/home/kkp/.config/hippocampus/hippocampus.env` | **密钥面**(`--env-file` 插值源,含全部 `:?required`) | **否** |
| `/home/kkp/.config/hippocampus/*.token` | 一次性下发 token 的 0600 文件 | **否** |

**Mac(客户端侧)**
| 路径 | 内容 |
|---|---|
| `~/.claude.json` → `mcpServers.hippocampus` | Claude Code 的 http MCP 配置(Bearer `${HIPPOCAMPUS_CLAUDE_TOKEN}`) |
| `~/.config/hippocampus/claude.token` | Claude 端 token,0600(唯一真值) |
| `~/.config/hippocampus/launch-claude.command` | 标准版 `/Applications/Claude.app` 的注入 wrapper |
| `~/Applications/Claude 分身.app/Contents/MacOS/launcher` | 分身(Claude-2)启动脚本,自注入 token |
| `~/Applications/Claude 分身GW.app/…/launcher` | 分身 GW(Claude-gw,3p 网关模式)启动脚本 |

---

## 5. 配置参考

### 5.1 密钥面 `hippocampus.env`（变量名,值不外泄）
`--env-file` **仅做插值**,不自动注入容器;每个密钥在 `compose.yaml` 里**逐服务显式映射**且带 `:?required`(缺失即启动失败)。模板见 `phase0/…/templates/hippocampus.env.example`(占位符)。变量:
- `HIPPOCAMPUS_MODE`(如 `commissioning`)
- `HIPPOCAMPUS_DB_PASSWORD`、`HINDSIGHT_INTERNAL_TOKEN`(映射进 MCP 为 `HIPPOCAMPUS_HINDSIGHT_TOKEN`)
- `HIPPOCAMPUS_TOKEN_PEPPER`、`HIPPOCAMPUS_AUDIT_HMAC_KEY`、`HIPPOCAMPUS_IDEMPOTENCY_HMAC_KEY`
- `CLIPROXY_OPENAI_BASE_URL`(完整 OpenAI 兼容 base,含且仅含一个 `/v1`,无 userinfo/query/fragment/尾斜杠)、`CLIPROXY_KEY`
- `PGVECTOR_DIGEST`、`HINDSIGHT_DIGEST`(拉取镜像的 digest 固定)
- `HIPPOCAMPUS_MCP_TAG`(**现网:本地构建镜像的 tag**;模板里写的是 `HIPPOCAMPUS_MCP_DIGEST`——以现网 TAG 为准)
- `KKP_UID`、`KKP_GID`(Pi5 上读取的数字 UID/GID)
- 三客户端 token(如按现网:`HIPPOCAMPUS_CLAUDE_TOKEN` / `HIPPOCAMPUS_CODEX_TOKEN` / `HIPPOCAMPUS_OPENCLAW_TOKEN`)——每个 ≥256bit 随机,**互不复用**;生成:`python3 -c "import secrets;print(secrets.token_urlsafe(32))"`。

### 5.2 `hindsight.env`（非密钥,`env_file` 挂载;关键项）
`STORE_DOCUMENT_TEXT=false`、`ENABLE_AUTO_CONSOLIDATION=false`、`MCP_ENABLED=false`、`ENABLE_API=true`、`ENABLE_CP=false`、`LLM_TRACE_ENABLED=false`、`OTEL_TRACES_ENABLED=false`、`AUDIT_LOG_ENABLED=false`;`LLM_OUTPUT_LANGUAGE=Chinese`、`QUERY_ANALYZER_LANGUAGES=en,zh`;retain 模型见 §13(现网 primary=`gpt-5.6-terra`、backup=`gemini-3.6-flash`);embedding=`paraphrase-multilingual-MiniLM-L12-v2`;reranker=`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`(fallback `rrf`);`RERANKER_MAX_CANDIDATES=100`(**延迟瓶颈,见 §13**)。数字 LLM 成员**不继承** primary 的 key。

### 5.3 镜像策略
- **db / hindsight**:`@sha256:` **digest 固定**(先核 `linux/arm64` manifest 再 pin)。
- **hippocampus-mcp**:**本地构建**,现网用 `${HIPPOCAMPUS_MCP_TAG}` 引用;prod 禁 `latest`;Dockerfile `production` 阶段剥除 failpoint 代码。

### 5.4 容器硬化(三容器均)
无 Docker socket / 管理 API;非 `privileged`;无 host PID/IPC/network;固定非 root UID/GID;`no-new-privileges:true`、`init:true`、`read_only:true` rootfs、精确可写挂载 + 限量 `/tmp` tmpfs、`cap_drop:[ALL]`;日志 json-file 限 size/file 数。

---

## 6. 首次部署（Phase 1 概要）

顺序(详见 `08-phases.md` + `phase1-pi5-deployment.md`):
1. 复跑停机前检查(`preflight-readonly.sh`、`verify-*.sh`)。
2. 建目录 0700/0600 + 密钥面(`hippocampus.env`)。
3. `hippocampus-registry init` → `issue` 各客户端 → `bind-peer` 建立 client→IP 绑定;**开 8888 之前**确保非空来源白名单 + peer 绑定就绪(§8.1)。
4. `docker compose -p hippocampus --env-file /home/kkp/.config/hippocampus/hippocampus.env up -d` 起三容器。
5. 建空 Bank `main` + 隔离 Bank `commissioning-v4-2`(bank 级 PATCH 锁 `store_document_text=false`+`audit_log_enabled=false`)。
6. 过 401/403 + HMAC + 白名单校验后,**一次性**下发 token(0600/凭据库,阅后即删)。
7. 分配数据集(**不导入**),核对现有网络无改动。

---

## 7. 迭代部署与升级

> 通则:改任何一层后跑 §7.4 验证清单;有条件先在**临时 Bank/实例**验证再上 `main`。

### 7.1 升级 MCP（本地镜像 —— 最常见)
现网镜像是打包型(源码不 bind-mount),改源码**必须重建 + 重部署**:

```bash
# 0) 改宿主源码(先备份、语法自检)
F=/home/kkp/hippocampus/hippocampus-mcp/src/hippocampus/<file>.py
cp -p "$F" "$F.bak.$(date +%Y%m%d-%H%M%S)"
# ...编辑...
python3 -m py_compile "$F"

# 1) 构建新 tag(干净重建,推荐单一来源)
cd /home/kkp/hippocampus/hippocampus-mcp
docker build --target production -t hippocampus-mcp:<新tag> .
#   —— 或快速叠层(仅换个别文件、免联网):
#   printf 'FROM hippocampus-mcp:<旧tag>\nCOPY --chown=1000:1000 src/hippocampus/<file>.py /app/src/hippocampus/<file>.py\n' \
#     | docker build -f - -t hippocampus-mcp:<新tag> .

# 2) 改 env 里的 TAG(先备份密钥面)
E=/home/kkp/.config/hippocampus/hippocampus.env
cp -p "$E" "$E.bak.$(date +%Y%m%d-%H%M%S)"
sed -i 's|^HIPPOCAMPUS_MCP_TAG=.*|HIPPOCAMPUS_MCP_TAG=<新tag>|' "$E"

# 3) 只重建 MCP 容器(db/hindsight 不动)
cd /home/kkp/hippocampus
docker compose -f compose.yaml --env-file "$E" up -d hippocampus-mcp

# 4) 验健康
docker inspect -f '{{.State.Health.Status}}' hippocampus-hippocampus-mcp-1
```
回滚 = TAG 改回旧值 + `up -d hippocampus-mcp`(旧镜像保留)。

### 7.2 升级 hindsight / pgvector（digest）
先核 `linux/arm64` manifest → 把新 `HINDSIGHT_DIGEST` / `PGVECTOR_DIGEST` 写进 env → 有条件先临时 Bank/实例验证 → `up -d hindsight`(或 `db`)。切勿用浮动 tag。

### 7.3 改 Hindsight 配置(模型 / reranker / 语言)
编辑 `/home/kkp/hippocampus/hindsight.env` → `docker compose … up -d hindsight`(config 是 `env_file`,重建容器即生效)。改模型/维度类需重验召回质量。

### 7.4 变更后统一验证清单
- `docker ps` 三容器 healthy;仅 `192.168.2.41:8888` 新监听。
- `curl` 未认证 → 401;带 token → 通过。
- 真实客户端 `memory_search` + `memory_read` 返回预期。
- 资源峰值仍在 3/1/0.512 GiB 内;现有业务(HA/Gotify/Backup/Caddy)无扰动。

---

## 8. 客户端接入

### 8.1 服务端下发(注册表 CLI)
CLI = `python -m hippocampus.registry_cli`,经 `docker exec` 在 MCP 容器内运行;`--db /data/state/outbox.db`;`HIPPOCAMPUS_TOKEN_PEPPER` 已在容器环境;**token 只走 stdin 或 0600 文件,绝不进 argv/stdout**。

```bash
C=hippocampus-hippocampus-mcp-1
DB=/data/state/outbox.db
# 发放(readable/writable 为逗号分隔 project;commissioning 期只给 commissioning)
TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
printf '%s' "$TOKEN" | docker exec -i "$C" python -m hippocampus.registry_cli --db "$DB" \
  issue mac-claude --source-tag mac-claude --readable commissioning --writable commissioning --expires-days 14
#   → 之后把 $TOKEN 一次性写入客户端 0600 文件,随即从此处清除
# 绑定来源 IP(开放 8888 前必须完成;union gate 逐请求读取最新快照)
docker exec "$C" python -m hippocampus.registry_cli --db "$DB" bind-peer mac-claude <MAC_LAN_IP>
# 其它:grant <id> --readable .. --writable ..(改授权)/ rotate <id>(换 token,走 stdin)/
#       revoke <id> / revoke-peer <id> <ip> / list-safe(只出安全元数据)
docker exec "$C" python -m hippocampus.registry_cli --db "$DB" list-safe
```
> 注:计划里的 `registry-*.sh` 包装脚本未随现网 `scripts/` 下发,直接用上面的模块调用。子命令:`init/issue/rotate/revoke/grant/bind-peer/revoke-peer/list-safe`(源:`registry_cli.py`)。

### 8.2 客户端侧配置(Mac)
`~/.claude.json`:
```json
"hippocampus": {
  "type": "http",
  "url": "http://192.168.2.41:8888/mcp/",
  "headers": { "Authorization": "Bearer ${HIPPOCAMPUS_CLAUDE_TOKEN}" }
}
```
token 存 `~/.config/hippocampus/claude.token`(0600),**不入 shell profile / launchctl / 配置明文**。让 token 进到读配置的进程环境:
- **标准版 `/Applications/Claude.app`**:经 wrapper `~/.config/hippocampus/launch-claude.command`(export token → 启动 app)。
- **分身(多开)**:各自 `launcher` 脚本内 `export HIPPOCAMPUS_CLAUDE_TOKEN="$(cat ~/.config/hippocampus/claude.token)"` 后**直启二进制**(`open` 不透传 env),且必须 `/usr/bin/arch -arm64`(脚本型 .app 会被 LaunchServices 以 x86_64 启动,否则 Electron+claude-code 全走 Rosetta 卡死)。`Claude 分身`=Claude-2 标准模式(`--user-data-dir`);`Claude 分身GW`=Claude-gw 3p 网关模式(`CLAUDE_USER_DATA_DIR`)。
- **禁用** `launchctl setenv`(会把 token 泄进全局 launchd 环境)。
- **Codex** 端同理:自己的 `HIPPOCAMPUS_CODEX_TOKEN` + 对应 MCP 配置,curl 不能替代真实端到端。

### 8.3 校验接入
完全退出目标客户端 → 经 wrapper/launcher 重启(MCP 在客户端启动时解析,不能热接)→ 新会话确认出现 4 个 `memory_*` 工具 → 跑一次 `memory_search`(限已授权 project,如 `commissioning`)。

---

## 9. MCP 工具与调用约定

4 个工具(`memory_commit` / `memory_read` / `memory_search` / `memory_status`),统一授权:search/read/status 的 project 须 ∈ `readable_projects`,commit 须 ∈ `writable_projects`;read/status 从 URI 解析 project 后**二次授权**;filter 只能收窄。

- **URI/Bank 路由**:`memory://shared/<project>/<id>` ↔ `vault/shared/<project>/<id>.md`;客户端**不能**传 Bank,由服务端运行模式路由(prod→`main`,commissioning→`commissioning-v4-2`)。
- **写入约束**:保留字段 `src/trust/scope/sensitivity/tags/uri/document_id` 由 agent 自报 → 整请求 `RESERVED_FIELD_FORBIDDEN`;服务端强制 `scope:shared`+`sensitivity:internal`,普通 agent 写死 `trust:agent`;`type ∈ {decision,procedure,fact,incident,preference,constraint,reference}`;字段限额见 `03-data-model.md`(`title`≤256、`summary`60–200、`retrieval_text`100–600token/硬顶800、`detail_body`≤128KiB、JSON-RPC 体≤256KiB)。
- **信封严格性(重要,见 §12)**:服务端只接受 `tools/call` 的 `params` 含 `name`/`arguments`(现已额外放行 spec 保留键 `_meta`);多余键 → `-32602 "rejected"`。
- **无 `memory_update`**:同知识换幂等键会并存,更新/合并/去重是 Phase 3。

---

## 10. 运维

- **健康**:`docker ps`;`/healthz`(存活)、`/readyz`(就绪,Vault/SQLite/scanner/审计任一挂即 503;仅 Hindsight 挂返回 200+degraded)。
- **日志**:`docker logs <容器>`(json-file 限量);应用层禁记 header/query/body/异常体。
- **资源**:硬上限 3/1/0.512 GiB;关注 Pi5 供电(`vcgencmd get_throttled`,见 §13)。
- **审计**:每个已认证请求恰好一行(含未认证拒绝行);导出用 `export-audit.sh`(计划交付)。
- **备份/持久化**:`vault/`、`state/`(含审计)、`hippocampus-pgdata` 卷默认保留;删除需单独授权。客户端备份只在其自身安全域,记录只留安全元数据。

---

## 11. 回滚

### 11.1 镜像回滚(最快)
env 里 `HIPPOCAMPUS_MCP_TAG` 改回旧 tag(或 `HINDSIGHT_DIGEST`/`PGVECTOR_DIGEST` 回旧 digest)→ `docker compose … up -d <服务>`。旧镜像本地保留即可秒回。

### 11.2 服务回滚(容器级,永不删数据)
`scripts/rollback-server.sh` 只认**环境回滚清单**,且实际对象须同时匹配清单的 project + **两个标签**,否则拒绝;`down` 永不带 `-v`:
```bash
cd /home/kkp/hippocampus
scripts/rollback-server.sh --manifest scripts/rollback-manifest-prod.env --dry-run   # 先看
scripts/rollback-server.sh --manifest scripts/rollback-manifest-prod.env --execute   # 执行
```
清单(`rollback-manifest-prod.env`,非密钥):`ROLLBACK_PROJECT=hippocampus`、`io.hippocampus.managed=true`、`io.hippocampus.plan=v4.2`、compose 路径、`--env-file` 路径、`ROLLBACK_EXPECT_PORT=192.168.2.41:8888`(down 后校验监听消失)。

### 11.3 客户端回滚
`scripts/rollback-clients.sh --record <TSV> [--dry-run|--execute]`;记录格式 `client_id<TAB>backup_id<TAB>backup_path<TAB>target_path`;只对 0600 备份生效,只打印安全元数据。

### 11.4 Token 撤销 / 轮换
优先 `hippocampus-registry revoke <client_id>`(只需 client_id);换 token 用 `rotate <client_id>`(新 token 走 stdin)。回滚顺序:先按 client_id 撤销三客户端 token、恢复客户端配置,再按 project 精确停 `hippocampus-mcp`、确认 8888 消失。

---

## 12. 故障排查（实战）

| 症状 | 根因 | 处理 |
|---|---|---|
| 某会话看不到 hippocampus 工具 | 进程环境无 `HIPPOCAMPUS_CLAUDE_TOKEN`(token 展开为空→客户端静默丢弃 server) | 经 wrapper/launcher 启动;`launchctl getenv` 为空是**设计使然**,勿用 `launchctl setenv` |
| 分身启动后仍无 token | 分身用 `open` 启动,**不透传 env** | 改 launcher 为直启二进制 + `arch -arm64` + 子壳内 export token(§8.2) |
| 分身 CPU 打满/卡死 | 脚本型 .app 被以 x86_64 启动,Electron+claude-code 走 Rosetta | launcher 必须 `/usr/bin/arch -arm64 <Claude bin>` |
| **每个** tool 调用 `-32602 "rejected"`(id:null),但 initialize/tools_list 正常 | 服务端 envelope 只允许 `params={name,arguments}`,而 MCP 客户端带 `_meta` | 已修:`server.py` 先 `params.pop("_meta",None)` 再校验 → 镜像 `v4.2-metafix`(§14) |
| `401 unauthorized` | token 缺失/错误/过期 | 核对 0600 token、`rotate`;检查 `--env-file` 是否含该 token 变量 |
| `403 forbidden` | 来源 IP 不在 union gate,或 peer 未绑定 | `bind-peer <client_id> <ip>`;确认该 IP 就是实际来源(直连不认 XFF) |
| 工具返回 project 授权错(`PROJECT_NOT_READABLE/WRITABLE`) | 该 client 的 readable/writable 不含此 project | `grant <id> --readable .. --writable ..`;commissioning/global 须显式授予 |
| search 很慢(p95≈11s) | Pi5 ARM 上 cross-encoder reranker 对 100 候选逐个推理 | 见 §13:降 `RERANKER_MAX_CANDIDATES` 或换 `rrf`(需重验召回) |
| `-32602 "unknown tool"` | 工具名拼错 | 用 `memory_commit/read/search/status` 之一 |

---

## 13. 已知问题与待决策

- **搜索/reranker 延迟——当前不达 §10.4**:search p50≈8.96s、**p95≈11.3s**(目标 ≤5s;read/commit/status 皆毫秒)。根因:Pi5 ARM CPU 上 reranker 占召回 ~99%。可逆 A/B:`RERANKER_MAX_CANDIDATES` 100→20 → 2.1–2.3s(已还原 100)。**待决**:降候选数 + 重验中文召回 / 重批 p95 阈值到实测基线 / Phase 3 换 `rrf`·PGroonga·VectorChord。
- **TLS vs LAN 明文**:Phase 1 用 LAN HTTP,Bearer 可被同网段嗅探。Go/No-Go 条 12 要求**二选一**:单独授权的内部 TLS(非 Caddy)并过三端信任验证,**或**书面接受可信 LAN 明文残余风险。**待决**。
- **Token 轮换**:三客户端 token 14 天过期;`dockerNode-openclaw` 在 Phase 2 前禁用且无 peer 绑定;commissioning 收口时须轮换 PoC token。⚠️ **cliproxy API key 曾在对话通道泄露**,建议纳入轮换范围。
- **中文召回质量(§10.4)未验**:Phase 0 mock 无真实 embedding/reranker,只证了检索管线;真实质量需真 Hindsight + 三端数据集导入。
- **Docker 管理面残余**:`beszel` 监控 agent 视同 root、可读全部密钥,**已接受**、缓解未实施。
- **Pi5 供电欠压**:`vcgencmd get_throttled` 恒为 `0x50000`(历史欠压位),负载中无活动限频、SoC 68.6°C;使 §10.8「无限频位」字面不满足,判为环境残留(建议官方 27W USB-C PD)。§10.8 资源/稳定其余**通过**(60.5min/940 请求/0 错误;峰值 db 115、hindsight 1670、MCP 94 MiB;零重启)。
- **retain 模型漂移**:计划 `gemini-3-flash`/`deepseek-v4-flash` 在 cliproxy 上不可用/不支持 json_schema;现网 primary 已切 **`gpt-5.6-terra`**、backup `gemini-3.6-flash`(对应 commit `bc9cf8b`)。backup 首调曾遇 429 配额,Phase 2 并发导入时复检。
- **Go/No-Go 未评估**;三端真实 E2E(Claude/Codex/OpenClaw)与数据集导入、commissioning 收口(撤销/轮换 token、移 commissioning 读写、切空 `main`)**待执行**。禁走捷径:agent 直连 Hindsight、先落库后安检、临时共享 token、通配来源白名单、用 curl 替代真实三端。

---

## 14. 当前状态快照 (2026-08-08)

- 三容器 healthy;MCP 镜像 = **`hippocampus-mcp:v4.2-metafix`**(含 `_meta` 信封修复);db/hindsight digest 固定运行。
- 数据全在 `commissioning` / Bank `commissioning-v4-2`;正式 `main` **保持空**。
- 本机 Claude(分身 Claude-2)真实 `memory_search`+`memory_read` **已验证可用**;`memory_commit`(写链)与三端并发 E2E、数据集导入**未跑**。
- Go/No-Go **未评估**;§10.4 延迟、TLS/明文、token 轮换为主要未决项。
- 备份点:`server.py.bak.20260808-215926`、`hippocampus.env.bak.20260808-220050`、旧镜像 `v4.2-5cee863` 保留。

---

## 附录 A：关键命令速查

```bash
# 现网健康
ssh kkp@192.168.2.41 'docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"'
# 未认证应为 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  http://192.168.2.41:8888/mcp/
# 部署三容器 / 单服务
docker compose -p hippocampus -f /home/kkp/hippocampus/compose.yaml \
  --env-file /home/kkp/.config/hippocampus/hippocampus.env up -d [hippocampus-mcp]
# 注册表(容器内)
docker exec hippocampus-hippocampus-mcp-1 python -m hippocampus.registry_cli --db /data/state/outbox.db list-safe
# 服务回滚(永不 -v)
/home/kkp/hippocampus/scripts/rollback-server.sh --manifest /home/kkp/hippocampus/scripts/rollback-manifest-prod.env --dry-run
```

## 附录 B：源码/文档索引
- 设计:`plan/v4.2-final-2026-08-08/00…11-*.md`(overview/scope/architecture/data-model/security/outbox-worker/deployment/mcp-tools-audit/phases/acceptance-gonogo/rollback-deliverables/step-acceptance)。
- 执行报告:`phase0/v4.2-final-2026-08-08/reports/{phase0-report,phase1-pi5-deployment,phase2-clients,phase2-load-test}.md`。
- 部署物:`phase0/v4.2-final-2026-08-08/templates/{compose.yaml,hindsight.env,hippocampus.env.example,scripts/}`;MCP 源码:`phase0/v4.2-final-2026-08-08/hippocampus-mcp/src/hippocampus/`。

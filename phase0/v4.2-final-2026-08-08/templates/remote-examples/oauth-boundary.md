# Phase 4 OAuth boundary（候选文档,一期不实施）

> 生成于 Phase 0,仅作为 Phase 4 评估输入。加载或实施需另行授权（[01 §1.3](../../../plan/v4.2-final-2026-08-08/01-scope-boundaries.md)）。

## 边界

- OAuth 2.1 / DCR / PKCE 客户端一律经**独立网关**接入,不复用 `.2.41` Claude CLI Gateway,也不与其共享端口、Token、会话或模型路由。
- 网关只反代 Hippocampus MCP 的 `/mcp/`;Hindsight API、PostgreSQL、Control Plane 与健康路由永不对外。
- 远程接入强制 TLS。Phase 1–2 的受信 LAN 明文 Bearer 例外**不继承**到 Phase 4（[04 §4.6](../../../plan/v4.2-final-2026-08-08/04-security.md)）。

## 身份与授权

- OAuth 主体映射到 registry 的 `client_id` 后,仍走与三个一期客户端**完全相同**的统一授权函数:`readable_projects` / `writable_projects` / `allowed_types` 分离,filter 只能缩小权限。
- 远程身份不得绕过 `client_sources` 绑定;trusted proxy 必须是精确地址,`X-Forwarded-For` 仅在 remote mode 且来源为该精确代理时才被采信。
- 在线 Web 默认只读;若开放写入,固定 `trust:unreviewed`,不得生成 `trust:agent` 或 `trust:curated`。

## 撤销

- Token 与 OAuth 授权都必须可按 `client_id` 单独撤销,且撤销不需要读取任何明文凭证。
- 远程接入的每个主体独立限流与独立撤销,单个主体被撤销不影响其他主体。

## 不变量

秘密红线、正文不进 Hindsight、一请求一条脱敏审计、审计先于响应这四条在 Phase 4 同样成立,不因远程接入放宽。

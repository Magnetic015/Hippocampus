"""Generate the fixed synthetic dataset + ground truth (plan 08 Phase 0 step 7).

Deterministic: no clock, no randomness, so the digest is stable across runs.
All content is synthetic and contains no real secrets.
"""

from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hippocampus-mcp" / "src"))
sys.path.insert(0, str(HERE.parent / "scanner"))

CLIENT_CYCLE = ["mac-claude", "mac-codex", "dockerNode-openclaw"]

TOPICS = [
    ("写入链路与原子落盘", "decision",
     "memory_commit 先完成授权、ingress 限型与秘密扫描,再建立幂等 reservation,"
     "随后以相邻 staging 文件、fsync 文件与父目录、atomic rename 完成 Markdown 落盘。",
     "2026-07-02T10:15:00+08:00"),
    ("Outbox 异步索引边界", "constraint",
     "单实例 worker 只把 frontmatter 的 retrieval_text 与安全 metadata 送入 Hindsight,"
     "Detail 与完整 Markdown 永不外发,异步边界固定在 Hippocampus Outbox。", None),
    ("秘密红线零落盘", "constraint",
     "命中秘密整笔拒绝,不脱敏保存、不留隔离副本,秘密值与指纹不进入 Vault、SQLite、"
     "Outbox、审计、Hindsight 或日志。", None),
    ("并集 gate 与 client 绑定", "procedure",
     "listener 启动前加载全部已启用 client 精确地址的非空并集 gate,认证后再按 registry "
     "复核 client 与 socket peer 绑定,不匹配固定拒绝 source_mismatch。",
     "2026-07-05T14:00:00+08:00"),
    ("审计一请求一行", "decision",
     "每个已认证 MCP HTTP 请求恰有一条脱敏审计;并集 gate 拒绝与认证失败各写一行未认证终态行,"
     "client_id 为空而 source_ip 记录 socket peer。", None),
    ("时间语义 unset", "fact",
     "Hindsight v0.9.0 省略 timestamp 会默认当前 UTC,因此无可靠事件时间必须显式发送字符串 "
     "unset,顶层 timestamp 与 metadata event_at 取同值。",
     "2026-06-20T08:00:00+08:00"),
    ("端口与监听边界", "fact",
     "一期唯一新增监听为 192.168.2.41:8888 映射容器 8080,Hindsight API、PostgreSQL 5432 "
     "与 Control Plane 9999 均不发布到主机。",
     "2026-07-10T09:00:00+08:00"),
    ("幂等冲突判定", "procedure",
     "同 client 同 UUIDv4 key 同规范化 payload HMAC 复用原事件;同 key 不同 payload 返回 "
     "409 HIPPOCAMPUS_IDEMPOTENCY_CONFLICT,不生成文件或事件。", None),
    ("扫描器故障 fail-closed", "constraint",
     "秘密扫描器不可用时四个数据工具、worker、retry 与 startup recovery 全部停止,"
     "只有 healthz、readyz、version 可报告故障。", None),
    ("DURABILITY_GAP 处置", "incident",
     "prepared 状态下 staging 与正式文件同时缺失时,reservation 与 Outbox 行均标 conflict "
     "并记 DURABILITY_GAP,readyz 返回 503,不猜测重建。",
     "2026-07-18T22:40:00+08:00"),
    ("lease 契约", "decision",
     "lease_owner 恒为 process_instance_id,reservation lease TTL 60 秒、Outbox lease "
     "TTL 300 秒,时钟统一取 SQLite unixepoch,过期后只能由原子 CAS 接管。", None),
    ("检索归并规则", "procedure",
     "Hindsight 一份 retrieval_text 可能被提取成多个 fact,服务端逐条校验后按 document_id "
     "归并,冲突整组丢弃,最终最多返回 5 个唯一文档卡片。", None),
    ("token 计量口径", "decision",
     "retrieval_text 以版本锁定的 tiktoken cl100k_base 计数,范围 100 到 600,硬上限 800;"
     "summary 与 title 按 Unicode scalar 计数。", None),
    ("commissioning 隔离", "constraint",
     "Go 前三端写入只允许 project commissioning 与隔离 Bank,正式 main 保持为空,"
     "验收后撤销 PoC Token 并移除 commissioning 权限。", None),
    ("global 显式授权", "constraint",
     "保留 project global 必须在 registry 分别显式授予读写,不因代表客户端身份自动获得,"
     "也不会自动并入其他 project 的召回结果。", None),
    ("回滚清单机制", "procedure",
     "rollback-server.sh 只认环境回滚清单,核对 project 与 managed、plan 双标签,"
     "不匹配一律拒绝,执行不带 -v 的精确 Compose down,命名卷默认保留。", None),
    ("资源上限", "fact",
     "Hindsight 3 GiB、PostgreSQL 1 GiB、Hippocampus MCP 与 worker 512 MiB,"
     "落实为 Compose 可验证硬上限,不创建 swapfile。",
     "2026-07-12T11:20:00+08:00"),
    ("只读 rootfs 与能力", "constraint",
     "三容器禁止 privileged 与 host namespace,MCP 与 Hindsight 使用固定非 root UID、"
     "no-new-privileges、cap_drop ALL 与只读 rootfs 加精确 writable mount。", None),
    ("OTEL 与 trace 关闭", "decision",
     "HINDSIGHT_API_OTEL_TRACES_ENABLED 与 LLM_TRACE 必须显式关闭,exporter endpoint "
     "与 headers 均不得出现在 service env,验证 llm_requests 无请求响应副本。", None),
    ("Vault 唯一映射", "fact",
     "稳定 URI memory://shared/项目/文档 唯一映射到 Vault 的 shared 项目目录下同名 md 文件,"
     "不存在 projects 或 inbox 等特例目录。", None),
    ("读取上限", "constraint",
     "memory_read 单次最多 2 篇,单 URI 上限 192 KiB,总响应上限 256 KiB,超限返回安全错误"
     "并建议拆分主题,不做静默截断。", None),
    ("JSON-RPC 限型", "procedure",
     "每个 HTTP 请求只接受一个 JSON-RPC object,拒绝 batch array、压缩 body、未知 "
     "Content-Type、重复键与尾随数据,body 上限 256 KiB、depth 上限 8。", None),
    ("恢复策略重扫", "procedure",
     "startup recovery 对 prepared 事件按当前扫描策略重扫,通过才补 rename 与 ready,"
     "命中秘密标 policy_blocked,hash 不符标 conflict。", None),
    ("孤儿文件处置", "constraint",
     "无 reservation 与 Outbox provenance 的 Vault 文件与 staging 一律计为 unowned_orphan,"
     "禁止读取、索引与补事件,也不自动删除。", None),
    ("降级不阻塞任务", "decision",
     "记忆中枢任何故障都不得阻塞 Agent 完成当前用户任务,Agent 应跳过记忆步骤继续执行。", None),
    ("三端身份分离", "fact",
     "mac-claude、mac-codex 与 dockerNode-openclaw 使用各自独立的 256 位随机 Token,"
     "服务端只保存 HMAC-SHA-256 加独立 pepper 的哈希。", None),
    ("Bank 配置锁定", "procedure",
     "main 与隔离 Bank 均显式锁定 store_document_text 为 false、audit_log_enabled 为 false,"
     "并在启动时读取 config 复核,普通 Agent 无法修改。", None),
    ("失败退避", "fact",
     "worker 指数退避从 5 秒起,上限 15 分钟,第 8 次失败后进入 dead,只保留状态与枚举错误码,"
     "不复制 payload。", None),
    ("审计导出", "procedure",
     "审计默认保留 90 天,JSONL 只导出到 state 下的 audit-export 目录,目录 0700、文件 0600,"
     "采用临时文件加 fsync 加 rename,不进 Vault 或远程端点。", None),
    ("Gateway 排除", "constraint",
     "一期不得把 192.168.2.41 的 Claude CLI Gateway 用作任何配置、路由、模型端点或测试目标,"
     "也不得探测其端口、进程或健康状态。", None),
    ("中文召回基线", "fact",
     "文本检索扩展使用 simple 作为兼容基线,一期以真实中文召回集验证,不足则记录为后续 "
     "PGroonga 或 VectorChord 评估项,不在正式 Bank 原地更换后端。", None),
    ("客户端备份边界", "procedure",
     "三客户端配置备份本体与完整性摘要只留在各自原安全域或独立 0700 非 Git 目录,"
     "项目交付物只记录安全备份 ID、路径、时间、权限与回滚结果。", None),
]

QUERIES = [
    ("原子重命名与目录 fsync 怎么做的", ["写入链路与原子落盘"]),
    ("落盘顺序 staging 同步", ["写入链路与原子落盘"]),
    ("正文会不会被送进索引引擎", ["Outbox 异步索引边界"]),
    ("Detail 是否外发给 LLM", ["Outbox 异步索引边界"]),
    ("命中密钥之后怎么处理", ["秘密红线零落盘"]),
    ("凭证泄漏拒绝策略", ["秘密红线零落盘"]),
    ("source allowlist 如何生效", ["并集 gate 与 client 绑定"]),
    ("event_at unset 是什么语义", ["时间语义 unset"]),
    ("8888 端口映射到哪里", ["端口与监听边界"]),
    ("HIPPOCAMPUS_IDEMPOTENCY_CONFLICT 什么时候返回", ["幂等冲突判定"]),
    ("DURABILITY_GAP 出现怎么办", ["DURABILITY_GAP 处置"]),
    ("lease TTL 是多少秒", ["lease 契约"]),
    ("retrieval_text token 上限", ["token 计量口径"]),
    ("Top 5 卡片是怎么去重的", ["检索归并规则"]),
    ("global project 权限规则", ["global 显式授权"]),
    ("worker retry backoff 多久", ["失败退避"]),
    ("audit 保留多少天", ["审计导出"]),
    ("memory read size limit", ["读取上限"]),
    ("batch request rejected reason", ["JSON-RPC 限型"]),
    ("container hardening readonly rootfs", ["只读 rootfs 与能力"]),
]


def build() -> dict:
    records = []
    for i, (title, type_, body, event_at) in enumerate(TOPICS):
        client = CLIENT_CYCLE[i % len(CLIENT_CYCLE)]
        summary = (f"{title}:{body}" * 3)[:150]
        retrieval = (
            f"{title}。{body} "
            "该条目属于 Pi5 192.168.2.41 上的 Hippocampus 记忆中枢一期实施范围,"
            "涉及 Hippocampus MCP、MD Vault、SQLite reservation 与 Outbox、"
            "单实例 index worker 以及 Hindsight v0.9.0 轻索引与召回的边界约定。"
            "稳定逻辑 URI 形如 memory://shared/commissioning/mem_20260808_SAMPLE,"
            "完整正文只保存在 MD Vault,索引只包含本检索文本与安全 metadata。"
        )
        records.append({
            "seq": i + 1, "assigned_client": client, "title": title, "type": type_,
            "summary": summary, "retrieval_text": retrieval, "event_at": event_at,
            "detail_body": f"{body} 本 Detail 为合成验收内容,不含任何实际秘密值。",
        })
    ground_truth = [{"query": q, "expected_titles": titles} for q, titles in QUERIES]
    return {"records": records, "ground_truth": ground_truth,
            "assignment": {c: [r["seq"] for r in records if r["assigned_client"] == c]
                           for c in CLIENT_CYCLE}}


def main() -> None:
    from hippocampus import constants as C
    from hippocampus.textlimits import scalar_len, token_count
    import secretscanner

    data = build()
    for rec in data["records"]:
        assert C.SUMMARY_MIN_SCALARS <= scalar_len(rec["summary"]) <= C.SUMMARY_MAX_SCALARS, rec["seq"]
        tokens = token_count(rec["retrieval_text"])
        assert C.RETRIEVAL_MIN_TOKENS <= tokens <= C.RETRIEVAL_HARD_MAX_TOKENS, (rec["seq"], tokens)
        for field in ("title", "summary", "retrieval_text", "detail_body"):
            assert not secretscanner.scan_text(rec[field]), (rec["seq"], field)
        rec["retrieval_tokens"] = tokens
    out = HERE / "dataset.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(json.dumps({"records": len(data["records"]),
                      "queries": len(data["ground_truth"]),
                      "assignment": {k: len(v) for k, v in data["assignment"].items()}}))


if __name__ == "__main__":
    main()

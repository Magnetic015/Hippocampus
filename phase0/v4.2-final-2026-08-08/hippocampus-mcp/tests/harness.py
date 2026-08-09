"""Disposable test harness: real server + mock Hindsight over loopback."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from hippocampus import constants as C, registry
from hippocampus.db import connect
from hippocampus.server import Env, build_server
from mock_hindsight import MockHindsight

CLIENTS = {
    "mac-claude": "T" + "claude" * 8,
    "mac-codex": "T" + "codex0" * 8,
    "dockerNode-openclaw": "T" + "openc0" * 8,
}
PEPPER = b"phase0-pepper-not-a-real-secret"
AUDIT_KEY = b"phase0-audit-key-not-a-real-secret"
IDEM_KEY = b"phase0-idem-key-not-a-real-secret"


class Lab:
    def __init__(self, tmp_path, *, mode=C.MODE_COMMISSIONING, with_worker=True,
                 readable=("commissioning",), writable=("commissioning",)):
        os.environ["HIPPOCAMPUS_TEST_BUILD"] = "1"
        self.tmp = tmp_path
        self.mock = MockHindsight()
        base = self.mock.start()
        self.db_path = str(tmp_path / "state" / "outbox.db")
        self.vault = str(tmp_path / "vault")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        from hippocampus.db import init_db
        init_db(self.db_path)
        conn = connect(self.db_path)
        try:
            for cid, token in CLIENTS.items():
                registry.create_client(
                    conn, cid, token, PEPPER, source_tag=cid,
                    readable=list(readable), writable=list(writable), types=list(C.TYPES))
                registry.bind_peer(conn, cid, "127.0.0.1")
        finally:
            conn.close()
        self.env = Env(db_path=self.db_path, vault_root=self.vault, mode=mode,
                       bind=("127.0.0.1", 0), hindsight_base=base,
                       hindsight_token="mock-internal-token", token_pepper=PEPPER,
                       audit_key=AUDIT_KEY, idem_key=IDEM_KEY, backoff_base=0.05)
        self.handle = build_server(self.env, with_worker=with_worker).start()
        self.url = f"http://127.0.0.1:{self.handle.port}/mcp/"

    def close(self):
        self.handle.stop()
        self.mock.stop()

    # ------------------------------------------------------------- raw HTTP
    def raw(self, body: bytes, *, token: str | None = None, headers: dict | None = None,
            method: str = "POST", url: str | None = None):
        hdrs = {"Content-Type": "application/json"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        hdrs.update(headers or {})
        req = urllib.request.Request(url or self.url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw else None)

    def rpc(self, method: str, params: dict | None = None, *, client="mac-claude",
            rpc_id="req-1", token: str | None = None):
        body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            body["params"] = params
        return self.raw(json.dumps(body).encode(), token=token or CLIENTS[client])

    def call(self, tool: str, arguments: dict, *, client="mac-claude", rpc_id="req-1"):
        status, payload = self.rpc("tools/call", {"name": tool, "arguments": arguments},
                                   client=client, rpc_id=rpc_id)
        if payload and "result" in payload:
            result = payload["result"]
            data = json.loads(result["content"][0]["text"])
            return status, data, result["isError"]
        if payload and "error" in payload:  # pre-dispatch rejection carries the safe code
            err = payload["error"]
            return status, {"code": err["message"] if isinstance(err, dict) else err}, True
        return status, payload, True

    # ------------------------------------------------------------- helpers
    def db(self):
        return connect(self.db_path)

    def audit_rows(self):
        conn = self.db()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM audit ORDER BY ts, rowid")]
        finally:
            conn.close()

    def drain_worker(self, timeout=8.0):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            conn = self.db()
            try:
                pending = conn.execute(
                    "SELECT COUNT(*) c FROM outbox WHERE status IN"
                    " ('prepared','ready','indexing','retry_wait')").fetchone()["c"]
            finally:
                conn.close()
            if pending == 0:
                return True
            time.sleep(0.05)
        return False


VALID_SUMMARY = (
    "本条记忆用于 Phase 0 合成验收：记录 Hippocampus MCP 在提交链路上的授权、扫描、"
    "落盘与索引行为，并覆盖幂等重放与故障降级路径，确保正文只存 Vault、索引只含检索文本。"
)
VALID_RETRIEVAL = (
    "Pi5 192.168.2.41 上的 Hippocampus MCP 使用 MD Vault 作为正文唯一事实来源。"
    "memory_commit 在统一授权、JSON-RPC ingress 限型和秘密扫描通过后建立幂等 reservation，"
    "并以相邻 staging 文件、文件和目录 fsync、atomic rename 完成 Markdown 落盘，随后返回 "
    "index_pending。单实例 worker 只把 retrieval_text 与安全 metadata 送入 Hindsight，"
    "Detail 与完整 Markdown 永不外发。Hindsight 故障时正文仍可安全写入 Vault，恢复后 Outbox "
    "自动补索引。稳定逻辑 URI 形如 memory://shared/commissioning/mem_20260808_0001，"
    "完整文件 hash 只留 SQLite，审计只记录安全枚举与脱敏字段路径。"
)


def commit_args(key: str, **over):
    args = {
        "idempotency_key": key,
        "title": "Phase 0 合成记忆：写入链路",
        "summary": VALID_SUMMARY,
        "retrieval_text": VALID_RETRIEVAL,
        "project": "commissioning",
        "type": "decision",
        "detail_body": "合成 Detail 正文,不含任何实际秘密值。",
    }
    args.update(over)
    return args

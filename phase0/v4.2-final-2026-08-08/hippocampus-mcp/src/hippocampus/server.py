"""Hippocampus MCP HTTP server: gate → auth → audit → ingress → tools (plan 04 §4.1)."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import audit, constants as C, ingress, registry, tools
from .audit import AuditError
from .db import connect, init_db
from .hindsight_client import HindsightClient

try:  # production images ship a stub; test builds ship the real module
    from . import failpoints  # noqa: F401
except ImportError:  # pragma: no cover
    pass


class Env:
    def __init__(self, *, db_path: str, vault_root: str, mode: str, bind: tuple[str, int],
                 hindsight_base: str, hindsight_token: str, token_pepper: bytes,
                 audit_key: bytes, idem_key: bytes, backoff_base: float = C.BACKOFF_BASE_S):
        import secretscanner

        from . import textlimits
        if mode not in (C.MODE_PRODUCTION, C.MODE_COMMISSIONING):
            raise ValueError("bad mode")
        self.db_path = db_path
        self.vault_root = vault_root
        self.mode = mode
        self.bank = C.BANK_MAIN if mode == C.MODE_PRODUCTION else C.BANK_COMMISSIONING
        self.bind = bind
        self.pid = uuid.uuid4().hex
        self.pepper = token_pepper
        self.audit_key = audit_key
        self.idem_key = idem_key
        self.spv = secretscanner.SCAN_POLICY_VERSION
        self.scan_text = secretscanner.scan_text
        self.scan_fields = secretscanner.scan_fields
        self.token_count = textlimits.token_count
        self.hindsight = HindsightClient(
            hindsight_base, hindsight_token,
            retain_timeout=float(os.environ.get("HIPPOCAMPUS_RETAIN_TIMEOUT_S", "300")),
            recall_timeout=float(os.environ.get("HIPPOCAMPUS_RECALL_TIMEOUT_S", "15")))
        self.backoff_base = backoff_base
        self.scanner_down = False
        self.index_backend_down = False
        self.health = {"durability_gap": False, "invariant_broken": False}
        self.counters = {"source_denied": 0, "auth_failed": 0, "audit_unavailable": 0}
        self._source_tags: frozenset[str] | None = None
        self._event_locks: dict[str, threading.Lock] = {}
        self._event_locks_mu = threading.Lock()

    def try_lock_event(self, event_id: str) -> bool:
        """In-process complement of the DB lease: only one in-flight request per
        event may run the file state machine (05 §5.2)."""
        with self._event_locks_mu:
            lock = self._event_locks.setdefault(event_id, threading.Lock())
        return lock.acquire(blocking=False)

    def unlock_event(self, event_id: str) -> None:
        with self._event_locks_mu:
            lock = self._event_locks.get(event_id)
        if lock is not None and lock.locked():
            lock.release()

    def known_source_tags(self) -> frozenset[str]:
        if self._source_tags is None:
            conn = connect(self.db_path)
            try:
                self._source_tags = frozenset(
                    r["source_tag"] for r in conn.execute("SELECT source_tag FROM clients"))
            finally:
                conn.close()
        return self._source_tags


class _Ctx:
    def __init__(self, request_id: str, client_row, conn):
        self.request_id = request_id
        self.client_row = client_row
        self.client_id = client_row["client_id"]
        self.source_tag = client_row["source_tag"]
        self.conn = conn
        self.audit_done = False
        self.query_hmac: str | None = None
        self.query_len: int | None = None


def _tool_schemas() -> list[dict]:
    return [
        {"name": "memory_search", "description": "Search memory cards (<=5) in one authorized project.",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"}, "project": {"type": "string"},
             "type": {"type": "string"}, "source": {"type": "string"}},
             "required": ["query", "project"]}},
        {"name": "memory_read", "description": "Read 1-2 full memory documents by memory:// URI.",
         "inputSchema": {"type": "object", "properties": {
             "uris": {"type": "array", "items": {"type": "string"}}}, "required": ["uris"]}},
        {"name": "memory_commit", "description": "Commit one durable memory object.",
         "inputSchema": {"type": "object", "properties": {
             "idempotency_key": {"type": "string"}, "title": {"type": "string"},
             "summary": {"type": "string"}, "retrieval_text": {"type": "string"},
             "project": {"type": "string"}, "type": {"type": "string"},
             "detail_body": {"type": "string"}, "event_at": {"type": "string"}},
             "required": ["idempotency_key", "title", "summary", "retrieval_text", "project", "type"]}},
        {"name": "memory_status", "description": "Query index state by document_id or idempotency_key.",
         "inputSchema": {"type": "object", "properties": {
             "document_id": {"type": "string"}, "idempotency_key": {"type": "string"}}}},
    ]


def make_handler(env: Env):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "hippocampus"
        sys_version = ""

        def log_message(self, *args):  # access log must not record header/query/body
            pass

        # ---------------------------------------------------------- responses
        def _send(self, status: int, payload: dict | None, extra_headers: dict | None = None):
            body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _rpc_error(self, status: int, rpc_id, code: int, message: str):
            self._send(status, {"jsonrpc": "2.0", "id": rpc_id,
                                "error": {"code": code, "message": message}})

        def _rpc_result(self, rpc_id, result: dict):
            self._send(200, {"jsonrpc": "2.0", "id": rpc_id, "result": result})

        # ------------------------------------------------------------- routes
        def do_GET(self):
            if self.path in ("/healthz", "/readyz", "/version"):
                return self._health(self.path)
            if self.path.startswith("/mcp"):
                return self._mcp("GET")
            self._send(404, {"error": "not_found"})

        def do_POST(self):
            if self.path.startswith("/mcp"):
                return self._mcp("POST")
            self._send(404, {"error": "not_found"})

        def do_DELETE(self):
            if self.path.startswith("/mcp"):
                return self._mcp("DELETE")
            self._send(404, {"error": "not_found"})

        # ------------------------------------------------------------- health
        def _health(self, path: str):
            peer = self.client_address[0]
            allowed = peer in ("127.0.0.1", "::1")
            if not allowed:
                try:
                    conn = connect(env.db_path)
                    try:
                        allowed = peer in registry.union_gate(conn)
                    finally:
                        conn.close()
                except Exception:
                    allowed = False
            if not allowed:
                return self._send(403, {"error": "forbidden"})
            if path == "/healthz":
                return self._send(200, {"status": "alive"})
            if path == "/version":
                return self._send(200, {"version": C.VERSION})
            down = env.scanner_down or env.health["durability_gap"] or env.health["invariant_broken"]
            try:
                conn = connect(env.db_path)
                conn.execute("SELECT 1")
                conn.close()
            except Exception:
                down = True
            if down:
                return self._send(503, {"status": "down"})
            if env.index_backend_down:
                return self._send(200, {"status": "degraded", "reason": "index_backend_unavailable"})
            return self._send(200, {"status": "ready"})

        # --------------------------------------------------------------- /mcp/
        def _mcp(self, http_method: str):
            start = time.monotonic()
            request_id = uuid.uuid4().hex
            peer = self.client_address[0]
            try:
                conn = connect(env.db_path)
            except Exception:
                env.counters["audit_unavailable"] += 1
                return self._send(503, {"error": C.E_UNAVAILABLE})
            try:
                try:
                    gate = registry.union_gate(conn)
                except Exception:
                    env.counters["source_denied"] += 1
                    return self._send(503, {"error": C.E_UNAVAILABLE})
                if peer not in gate:
                    env.counters["source_denied"] += 1
                    try:
                        audit.insert_unauth(conn, request_id, env.pid, peer, C.OUTCOME_SOURCE_DENIED)
                    except AuditError:
                        pass
                    return self._send(403, {"error": "forbidden"})

                header = self.headers.get("Authorization", "")
                token = header[7:] if header.startswith("Bearer ") else ""
                client_row = registry.find_client_by_token(conn, token, env.pepper) if token else None
                del header, token
                if client_row is None:
                    env.counters["auth_failed"] += 1
                    try:
                        audit.insert_unauth(conn, request_id, env.pid, peer, C.OUTCOME_AUTH_FAILED)
                    except AuditError:
                        pass
                    return self._send(401, {"error": "unauthorized"})

                try:
                    audit.insert_started(conn, request_id, env.pid, client_row["client_id"], env.spv)
                except AuditError:
                    env.counters["audit_unavailable"] += 1
                    return self._send(503, {"error": C.E_AUDIT_UNAVAILABLE})

                if not registry.peer_bound(conn, client_row["client_id"], peer):
                    self._finalize_quiet(conn, request_id, "rejected", C.OUTCOME_SOURCE_MISMATCH)
                    return self._send(403, {"error": "forbidden"})

                ctx = _Ctx(request_id, client_row, conn)
                if http_method == "GET":
                    audit.set_envelope(conn, request_id, "session_get", None)
                    self._finalize_quiet(conn, request_id, "ok", "session_get")
                    return self._send(204, None)
                if http_method == "DELETE":
                    audit.set_envelope(conn, request_id, "session_delete", None)
                    self._finalize_quiet(conn, request_id, "ok", "session_delete")
                    return self._send(204, None)
                return self._mcp_post(ctx, start)
            finally:
                conn.close()

        def _finalize_quiet(self, conn, request_id, state, outcome, **kw) -> bool:
            try:
                audit.finalize(conn, request_id, state, outcome, **kw)
                return True
            except AuditError:
                env.counters["audit_unavailable"] += 1
                return False

        def _mcp_post(self, ctx: _Ctx, start: float):
            conn = ctx.conn
            if self.headers.get("Content-Encoding"):
                self._finalize_quiet(conn, ctx.request_id, "rejected", "envelope_rejected")
                return self._rpc_error(415, None, -32600, "rejected")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                self._finalize_quiet(conn, ctx.request_id, "rejected", "envelope_rejected")
                return self._rpc_error(415, None, -32600, "rejected")
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > C.MAX_BODY_BYTES:
                self._finalize_quiet(conn, ctx.request_id, "rejected", "envelope_rejected")
                return self._rpc_error(413, None, -32600, "rejected")
            raw = self.rfile.read(length)

            try:
                envelope = ingress.parse_envelope(raw)
            except ingress.IngressError:
                self._finalize_quiet(conn, ctx.request_id, "rejected", "envelope_rejected")
                return self._rpc_error(400, None, -32600, "rejected")

            if env.scanner_down:
                self._finalize_quiet(conn, ctx.request_id, "rejected", "scanner_unavailable")
                return self._rpc_error(503, None, -32000, C.E_SCAN_UNAVAILABLE)
            if any(env.scan_text(t) for t in ingress.envelope_scan_texts(envelope)):
                self._finalize_quiet(conn, ctx.request_id, "rejected", C.OUTCOME_SECRET_REJECTED)
                return self._rpc_error(400, None, -32600, "rejected")

            rpc_id = envelope["id"]
            operation = ingress.map_operation(envelope["method"])
            if operation is None:
                self._finalize_quiet(conn, ctx.request_id, "rejected", "invalid_operation")
                return self._rpc_error(404, None, -32601, "method not found")

            tool_enum = None
            if operation == "tools_call":
                tool_enum, _ = ingress.map_tool(envelope["params"].get("name"))
            audit.set_envelope(conn, ctx.request_id, operation, tool_enum)

            latency = lambda: int((time.monotonic() - start) * 1000)  # noqa: E731

            if operation == "initialize":
                if not self._finalize_quiet(conn, ctx.request_id, "ok", "initialize",
                                            latency_ms=latency()):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_result(rpc_id, {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "hippocampus", "version": C.VERSION},
                    "capabilities": {"tools": {}}})
            if operation == "initialized":
                self._finalize_quiet(conn, ctx.request_id, "ok", "initialized",
                                     latency_ms=latency())
                return self._send(202, None)
            if operation == "tools_list":
                if not self._finalize_quiet(conn, ctx.request_id, "ok", "tools_list",
                                            latency_ms=latency()):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_result(rpc_id, {"tools": _tool_schemas()})

            # tools_call
            if tool_enum == C.INVALID_TOOL:
                self._finalize_quiet(conn, ctx.request_id, "rejected", C.INVALID_TOOL,
                                     latency_ms=latency())
                return self._rpc_error(400, rpc_id, -32602, "unknown tool")
            params = envelope["params"]
            if set(params) - {"name", "arguments"} or not isinstance(params.get("arguments", {}), dict):
                self._finalize_quiet(conn, ctx.request_id, "rejected", "envelope_rejected",
                                     latency_ms=latency())
                return self._rpc_error(400, None, -32602, "rejected")
            arguments = params.get("arguments", {})

            try:
                payload = tools.DISPATCH[tool_enum](env, ctx, arguments)
            except tools.ToolError as err:
                if not ctx.audit_done:
                    self._finalize_quiet(conn, ctx.request_id, err.state, err.outcome,
                                         latency_ms=latency(),
                                         query_hmac=ctx.query_hmac, query_len=ctx.query_len)
                body = err.payload()
                body.pop("http_status", None)
                return self._rpc_result(rpc_id, _tool_text(body, is_error=True))
            except Exception:
                self._finalize_quiet(conn, ctx.request_id, "error", "internal_error",
                                     latency_ms=latency())
                return self._rpc_error(500, None, -32000, "internal error")

            if not ctx.audit_done:
                ok = self._finalize_quiet(conn, ctx.request_id, "ok", tool_enum,
                                          latency_ms=latency(),
                                          query_hmac=ctx.query_hmac, query_len=ctx.query_len)
                if not ok:  # discard results: audit-before-response (R8)
                    return self._rpc_result(rpc_id, _tool_text(
                        {"code": C.E_AUDIT_UNAVAILABLE, "retryable": True}, is_error=True))
            return self._rpc_result(rpc_id, _tool_text(payload, is_error=False))

    return Handler


def _tool_text(payload: dict, *, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error}


class ServerHandle:
    def __init__(self, env: Env, httpd: ThreadingHTTPServer, worker):
        self.env = env
        self.httpd = httpd
        self.worker = worker
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self):
        self.thread.start()
        if self.worker:
            self.worker.start()
        return self

    def stop(self):
        if self.worker:
            self.worker.stop()
        self.httpd.shutdown()
        self.httpd.server_close()


def build_server(env: Env, *, with_worker: bool = True, run_recovery: bool = True) -> ServerHandle:
    from .recovery import run_startup_recovery
    from .worker import Worker

    init_db(env.db_path)
    os.makedirs(env.vault_root, exist_ok=True)
    if run_recovery:
        run_startup_recovery(env)
    conn = connect(env.db_path)
    try:
        gate = registry.union_gate(conn)
    finally:
        conn.close()
    if not gate:
        raise SystemExit("refusing to start: empty source union gate (plan 04 §4.6)")
    env.token_count("warmup")  # token counter must be available before listener
    httpd = ThreadingHTTPServer(env.bind, make_handler(env))
    worker = Worker(env, poll_interval=0.05) if with_worker else None
    return ServerHandle(env, httpd, worker)


def main() -> None:  # pragma: no cover - manual entry
    e = os.environ
    env = Env(
        db_path=e["HIPPOCAMPUS_DB"],
        vault_root=e["HIPPOCAMPUS_VAULT"],
        mode=e.get("HIPPOCAMPUS_MODE", C.MODE_COMMISSIONING),
        bind=(e.get("HIPPOCAMPUS_BIND_HOST", "0.0.0.0"), int(e.get("HIPPOCAMPUS_BIND_PORT", "8080"))),
        hindsight_base=e["HIPPOCAMPUS_HINDSIGHT_BASE"],
        hindsight_token=e["HIPPOCAMPUS_HINDSIGHT_TOKEN"],
        token_pepper=e["HIPPOCAMPUS_TOKEN_PEPPER"].encode(),
        audit_key=e["HIPPOCAMPUS_AUDIT_HMAC_KEY"].encode(),
        idem_key=e["HIPPOCAMPUS_IDEMPOTENCY_HMAC_KEY"].encode(),
    )
    handle = build_server(env)
    handle.start()
    handle.thread.join()


if __name__ == "__main__":  # pragma: no cover
    main()

"""Hippocampus MCP HTTP server: gate → auth → audit → ingress → tools (plan 04 §4.1)."""

from __future__ import annotations

import json
import os
import socket
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


class _HeaderDeadlineReader:
    """Buffered request reader with one absolute deadline for all headers.

    ``socket.settimeout`` alone is only an idle timeout: a peer can keep a
    request thread alive by sending one byte before every timeout.  This reader
    keeps bytes read past a newline for the normal body reader while recomputing
    the remaining wall-clock budget before each underlying read.
    """

    def __init__(self, base, connection):
        self._base = base
        self._connection = connection
        self._buffer = bytearray()
        self._deadline: float | None = None
        self._previous_timeout = None

    def start_deadline(self, timeout_s: float) -> None:
        self.clear_deadline()
        self._previous_timeout = self._connection.gettimeout()
        self._deadline = time.monotonic() + timeout_s

    def clear_deadline(self) -> None:
        if self._deadline is None:
            return
        previous_timeout = self._previous_timeout
        self._deadline = None
        self._previous_timeout = None
        try:
            self._connection.settimeout(previous_timeout)
        except OSError:
            pass

    def _read_base1(self, size: int) -> bytes:
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("request header deadline exceeded")
            self._connection.settimeout(remaining)
        return self._base.read1(size)

    def read1(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if self._buffer:
            count = len(self._buffer) if size is None or size < 0 else min(size, len(self._buffer))
            chunk = bytes(self._buffer[:count])
            del self._buffer[:count]
            return chunk
        return self._read_base1(size)

    def readline(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        limit = None if size is None or size < 0 else size
        while True:
            available = len(self._buffer) if limit is None else min(len(self._buffer), limit)
            newline = self._buffer.find(b"\n", 0, available)
            if newline >= 0:
                end = newline + 1
                line = bytes(self._buffer[:end])
                del self._buffer[:end]
                return line
            if limit is not None and len(self._buffer) >= limit:
                line = bytes(self._buffer[:limit])
                del self._buffer[:limit]
                return line
            read_size = 8192 if limit is None else min(8192, limit - len(self._buffer))
            chunk = self._read_base1(read_size)
            if not chunk:
                line = bytes(self._buffer)
                self._buffer.clear()
                return line
            self._buffer.extend(chunk)

    def __getattr__(self, name):
        return getattr(self._base, name)


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
        self.health = {
            "durability_gap": False,
            "invariant_broken": False,
            "audit_sink_down": False,
            "audit_retention_failed": False,
        }
        self.counters = {"source_denied": 0, "auth_failed": 0, "audit_unavailable": 0}
        self._event_locks: dict[str, threading.Lock] = {}
        self._event_locks_mu = threading.Lock()
        self._audit_retention_mu = threading.Lock()
        self._audit_retention_last_attempt: float | None = None

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
        # Registry changes are made by a separate CLI process, so a process-local
        # cache would reject newly issued, already valid source tags until restart.
        conn = connect(self.db_path)
        try:
            return frozenset(r["source_tag"] for r in conn.execute(
                "SELECT source_tag FROM clients"))
        finally:
            conn.close()

    def audit_failed(self) -> None:
        self.health["audit_sink_down"] = True
        self.counters["audit_unavailable"] += 1

    def audit_recovered(self) -> None:
        self.health["audit_sink_down"] = False

    def maintain_audit_retention(
            self, *, force: bool = False, monotonic_now: float | None = None) -> bool:
        """Run retention at startup or when the hourly maintenance gate is due."""
        attempt_at = time.monotonic() if monotonic_now is None else monotonic_now
        if not self._audit_retention_mu.acquire(blocking=False):
            return not self.health["audit_retention_failed"]
        conn = None
        try:
            last = self._audit_retention_last_attempt
            interval = (C.AUDIT_RETENTION_RETRY_INTERVAL_S
                        if self.health["audit_retention_failed"]
                        else C.AUDIT_RETENTION_INTERVAL_S)
            if (not force and last is not None
                    and attempt_at - last < interval):
                return not self.health["audit_retention_failed"]
            # Record attempts, including failures, so a persistent failure cannot
            # turn frequent readiness traffic into an unbounded write loop.
            self._audit_retention_last_attempt = attempt_at
            conn = connect(self.db_path)
            audit.enforce_retention(conn)
            self.health["audit_retention_failed"] = False
            self.audit_recovered()
            return True
        except Exception:
            self.health["audit_retention_failed"] = True
            self.audit_failed()
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._audit_retention_mu.release()


class _Ctx:
    def __init__(self, request_id: str, client_row, conn, bearer_token: str):
        self.request_id = request_id
        self.client_row = client_row
        self.client_id = client_row["client_id"]
        self.source_tag = client_row["source_tag"]
        self.conn = conn
        self.audit_done = False
        self.query_hmac: str | None = None
        self.query_len: int | None = None
        self._request_token: bytearray | None = bytearray(bearer_token, "utf-8")

    def business_value_contains_token(self, value: str) -> bool:
        token = self._request_token
        if not token:
            return False
        return value.encode("utf-8", errors="surrogatepass").find(token) >= 0

    def clear_request_token(self) -> None:
        token = self._request_token
        if token is not None:
            token[:] = b"\x00" * len(token)
            self._request_token = None


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


def _probe_audit_sink(env: Env) -> bool:
    """Commit a no-residue audit-table write to prove the sink is writable."""
    conn = None
    probe_id = f"ready-{uuid.uuid4().hex}"
    try:
        conn = connect(env.db_path)
        # A health request must complete before the container probe timeout even
        # if another writer is wedged.
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO audit (request_id, ts, process_instance_id, state, outcome_code)"
            " VALUES (?,?,?,?,?)",
            (probe_id, int(time.time()), env.pid, "ok", "readiness_probe"),
        )
        conn.execute("DELETE FROM audit WHERE request_id=?", (probe_id,))
        conn.execute("COMMIT")
        env.audit_recovered()
        return True
    except Exception:
        env.health["audit_sink_down"] = True
        if conn is not None:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except Exception:
                pass
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def make_handler(env: Env):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "hippocampus"
        sys_version = ""

        def log_message(self, *args):  # access log must not record header/query/body
            pass

        def setup(self):
            super().setup()
            self.rfile = _HeaderDeadlineReader(self.rfile, self.connection)

        def handle_one_request(self):
            self.rfile.start_deadline(C.REQUEST_HEADER_TIMEOUT_S)
            try:
                return super().handle_one_request()
            finally:
                self.rfile.clear_deadline()

        def parse_request(self):
            try:
                return super().parse_request()
            finally:
                # The request line and every header share one deadline.  Clear it
                # before dispatch so the body receives its own absolute budget.
                self.rfile.clear_deadline()

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

        def _rpc_result(self, rpc_id, result: dict, *, status: int = 200):
            self._send(status, {"jsonrpc": "2.0", "id": rpc_id, "result": result})

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
            down = (env.scanner_down
                    or env.health["durability_gap"]
                    or env.health["invariant_broken"]
                    or env.health["audit_retention_failed"]
                    or not _probe_audit_sink(env))
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
                env.audit_failed()
                return self._send(503, {"error": C.E_UNAVAILABLE})
            ctx = None
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
                        env.audit_failed()
                    return self._send(403, {"error": "forbidden"})

                header = self.headers.get("Authorization", "")
                token = header[7:] if header.startswith("Bearer ") else ""
                client_row = registry.find_client_by_token(conn, token, env.pepper) if token else None
                if client_row is None:
                    del header, token
                    env.counters["auth_failed"] += 1
                    try:
                        audit.insert_unauth(conn, request_id, env.pid, peer, C.OUTCOME_AUTH_FAILED)
                    except AuditError:
                        env.audit_failed()
                    return self._send(401, {"error": "unauthorized"})
                ctx = _Ctx(request_id, client_row, conn, token)
                del header, token

                try:
                    audit.insert_started(conn, request_id, env.pid, client_row["client_id"], env.spv)
                except AuditError:
                    env.audit_failed()
                    return self._send(503, {"error": C.E_AUDIT_UNAVAILABLE})

                if not registry.peer_bound(conn, client_row["client_id"], peer):
                    if not self._finalize_quiet(
                            conn, request_id, "rejected", C.OUTCOME_SOURCE_MISMATCH):
                        return self._send(503, {"error": C.E_AUDIT_UNAVAILABLE})
                    return self._send(403, {"error": "forbidden"})

                if http_method == "GET":
                    try:
                        audit.set_envelope(conn, request_id, "session_get", None)
                    except AuditError:
                        env.audit_failed()
                        self._finalize_quiet(conn, request_id, "error", "audit_unavailable")
                        return self._send(503, {"error": C.E_AUDIT_UNAVAILABLE})
                    if not self._finalize_quiet(conn, request_id, "ok", "session_get"):
                        return self._send(503, {"error": C.E_AUDIT_UNAVAILABLE})
                    return self._send(204, None)
                if http_method == "DELETE":
                    try:
                        audit.set_envelope(conn, request_id, "session_delete", None)
                    except AuditError:
                        env.audit_failed()
                        self._finalize_quiet(conn, request_id, "error", "audit_unavailable")
                        return self._send(503, {"error": C.E_AUDIT_UNAVAILABLE})
                    if not self._finalize_quiet(conn, request_id, "ok", "session_delete"):
                        return self._send(503, {"error": C.E_AUDIT_UNAVAILABLE})
                    return self._send(204, None)
                return self._mcp_post(ctx, start)
            finally:
                if ctx is not None:
                    ctx.clear_request_token()
                conn.close()

        def _finalize_quiet(self, conn, request_id, state, outcome, **kw) -> bool:
            try:
                audit.finalize(conn, request_id, state, outcome, **kw)
                env.audit_recovered()
                return True
            except AuditError:
                env.audit_failed()
                return False

        def _read_request_body(self, length: int) -> bytes:
            previous_timeout = self.connection.gettimeout()
            deadline = time.monotonic() + C.REQUEST_BODY_TIMEOUT_S
            chunks: list[bytes] = []
            remaining = length
            try:
                # A single socket timeout is only an idle timeout: a peer can keep
                # the thread alive indefinitely by dripping one byte before each
                # expiry.  Recompute the remaining wall-clock budget for every raw
                # buffered read so the whole body has one absolute deadline.
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        raise TimeoutError("request body deadline exceeded")
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(remaining)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes) or len(chunk) > remaining:
                        raise OSError("invalid request body read")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                return b"".join(chunks)
            finally:
                try:
                    self.connection.settimeout(previous_timeout)
                except OSError:
                    self.close_connection = True

        def _mcp_post(self, ctx: _Ctx, start: float):
            conn = ctx.conn
            if self.headers.get("Content-Encoding"):
                self.close_connection = True  # unread body must not enter the next request
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "envelope_rejected"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(415, None, -32600, "rejected")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                self.close_connection = True
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "envelope_rejected"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(415, None, -32600, "rejected")

            length_header = self.headers.get("Content-Length")
            if (not isinstance(length_header, str)
                    or not length_header.isascii()
                    or not length_header.isdigit()
                    or len(length_header) > 20):
                self.close_connection = True
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "envelope_rejected"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(400, None, -32600, "rejected")
            try:
                length = int(length_header)
            except ValueError:  # defense in depth for interpreter conversion limits
                self.close_connection = True
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "envelope_rejected"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(400, None, -32600, "rejected")
            if length <= 0 or length > C.MAX_BODY_BYTES:
                self.close_connection = True
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "envelope_rejected"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(413, None, -32600, "rejected")
            try:
                raw = self._read_request_body(length)
            except (socket.timeout, TimeoutError):
                self.close_connection = True
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "body_read_timeout"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(408, None, -32600, "rejected")
            except OSError:
                self.close_connection = True
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "body_read_failed"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(400, None, -32600, "rejected")
            if not isinstance(raw, bytes) or len(raw) != length:
                self.close_connection = True
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "body_read_incomplete"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(400, None, -32600, "rejected")

            try:
                envelope = ingress.parse_envelope(raw)
            except ingress.IngressError:
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "envelope_rejected"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(400, None, -32600, "rejected")

            if env.scanner_down:
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "scanner_unavailable"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(503, None, -32000, C.E_SCAN_UNAVAILABLE)
            try:
                tools.reject_envelope_request_token(ctx, envelope)
            except tools.ToolError as err:
                ok = self._finalize_quiet(
                    conn, ctx.request_id, err.state, err.outcome,
                    redacted_fields=err.extra.get("fields"),
                    redacted_categories=err.extra.get("categories"))
                if not ok:
                    return self._rpc_result(None, _tool_text(
                        {"code": C.E_AUDIT_UNAVAILABLE, "retryable": True}, is_error=True))
                body = err.payload()
                http_status = body.pop("http_status", 200)
                return self._rpc_result(
                    None, _tool_text(body, is_error=True), status=http_status)
            if any(env.scan_text(t) for t in ingress.envelope_scan_texts(envelope)):
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", C.OUTCOME_SECRET_REJECTED):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(400, None, -32600, "rejected")

            rpc_id = envelope["id"]
            operation = ingress.map_operation(envelope["method"])
            if operation is None:
                if not self._finalize_quiet(
                        conn, ctx.request_id, "rejected", "invalid_operation"):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(404, None, -32601, "method not found")

            tool_enum = None
            if operation == "tools_call":
                tool_enum, _ = ingress.map_tool(envelope["params"].get("name"))
            try:
                audit.set_envelope(conn, ctx.request_id, operation, tool_enum)
            except AuditError:
                env.audit_failed()
                self._finalize_quiet(conn, ctx.request_id, "error", "audit_unavailable")
                return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)

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
                if not self._finalize_quiet(conn, ctx.request_id, "ok", "initialized",
                                            latency_ms=latency()):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._send(202, None)
            if operation == "tools_list":
                if not self._finalize_quiet(conn, ctx.request_id, "ok", "tools_list",
                                            latency_ms=latency()):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_result(rpc_id, {"tools": _tool_schemas()})

            # tools_call
            if tool_enum == C.INVALID_TOOL:
                if not self._finalize_quiet(conn, ctx.request_id, "rejected", C.INVALID_TOOL,
                                            latency_ms=latency()):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(400, rpc_id, -32602, "unknown tool")
            params = dict(envelope["params"])
            params.pop("_meta", None)  # MCP-reserved metadata; discard, do not process
            if set(params) - {"name", "arguments"} or not isinstance(params.get("arguments", {}), dict):
                if not self._finalize_quiet(conn, ctx.request_id, "rejected", "envelope_rejected",
                                            latency_ms=latency()):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
                return self._rpc_error(400, None, -32602, "rejected")
            arguments = params.get("arguments", {})

            try:
                tools.reject_request_token(ctx, arguments)
                payload = tools.DISPATCH[tool_enum](env, ctx, arguments)
            except tools.ToolError as err:
                if not ctx.audit_done:
                    ok = self._finalize_quiet(
                        conn, ctx.request_id, err.state, err.outcome,
                        latency_ms=latency(), query_hmac=ctx.query_hmac,
                        query_len=ctx.query_len,
                        redacted_fields=err.extra.get("fields"),
                        redacted_categories=err.extra.get("categories"))
                    if not ok:
                        return self._rpc_result(rpc_id, _tool_text(
                            {"code": C.E_AUDIT_UNAVAILABLE, "retryable": True},
                            is_error=True))
                body = err.payload()
                http_status = body.pop("http_status", 200)
                return self._rpc_result(
                    rpc_id, _tool_text(body, is_error=True), status=http_status)
            except Exception:
                if not self._finalize_quiet(conn, ctx.request_id, "error", "internal_error",
                                            latency_ms=latency()):
                    return self._rpc_error(503, None, -32000, C.E_AUDIT_UNAVAILABLE)
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
        self._maintenance_stop = threading.Event()
        self._maintenance_thread = threading.Thread(
            target=self._audit_retention_loop, daemon=True)

    def _audit_retention_loop(self) -> None:
        while True:
            interval = (C.AUDIT_RETENTION_RETRY_INTERVAL_S
                        if self.env.health["audit_retention_failed"]
                        else C.AUDIT_RETENTION_INTERVAL_S)
            if self._maintenance_stop.wait(interval):
                return
            self.env.maintain_audit_retention()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self):
        self.thread.start()
        self._maintenance_thread.start()
        if self.worker:
            self.worker.start()
        return self

    def stop(self):
        self._maintenance_stop.set()
        self._maintenance_thread.join(timeout=5)
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
    # Retention is enforced before the listener opens, then by the hourly
    # maintenance loop.  Failures are contained and reflected by /readyz.
    env.maintain_audit_retention(force=True)
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

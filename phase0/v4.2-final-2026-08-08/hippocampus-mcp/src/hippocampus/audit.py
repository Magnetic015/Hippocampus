"""Operation audit (plan 07 §7.6, F1): one row per request, pre-auth rows included."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3

from . import failpoints
from .constants import AUDIT_TERMINAL, OUTCOME_FINALIZE_LOST
from .db import now


class AuditError(Exception):
    pass


def _exec(conn: sqlite3.Connection, sql: str, args: tuple, fp: str | None = None) -> None:
    """Any sink failure — real or injected — surfaces as AuditError."""
    try:
        if fp:
            failpoints.hit(fp)
        conn.execute(sql, args)
    except sqlite3.Error as exc:
        raise AuditError(str(exc.__class__.__name__)) from exc


def insert_started(conn, request_id: str, pid: str, client_id: str, spv: str) -> None:
    _exec(conn,
          "INSERT INTO audit (request_id, ts, process_instance_id, client_id, state, scan_policy_version)"
          " VALUES (?,?,?,?, 'started', ?)",
          (request_id, now(), pid, client_id, spv), fp="audit.insert")


def insert_unauth(conn, request_id: str, pid: str, source_ip: str, outcome: str) -> None:
    """Gate denial / auth failure: single terminal row, client_id NULL, source_ip set."""
    _exec(conn,
          "INSERT INTO audit (request_id, ts, process_instance_id, client_id, source_ip, state, outcome_code)"
          " VALUES (?,?,?, NULL, ?, 'rejected', ?)",
          (request_id, now(), pid, source_ip, outcome))


def set_envelope(conn, request_id: str, operation: str, tool: str | None) -> None:
    _exec(conn, "UPDATE audit SET operation_enum=?, tool_enum=? WHERE request_id=?",
          (operation, tool, request_id))


def link_event(conn, request_id: str, root_request_id: str, event_id: str) -> None:
    _exec(conn, "UPDATE audit SET root_request_id=?, event_id=? WHERE request_id=?",
          (root_request_id, event_id, request_id))


def set_state(conn, request_id: str, state: str, outcome: str | None = None) -> None:
    _exec(conn, "UPDATE audit SET state=?, outcome_code=COALESCE(?, outcome_code) WHERE request_id=?",
          (state, outcome, request_id), fp=f"audit.state.{state}")


def finalize(conn, request_id: str, state: str, outcome: str | None,
             latency_ms: int | None = None, normalized_ref: str | None = None,
             query_hmac: str | None = None, query_len: int | None = None) -> None:
    assert state in AUDIT_TERMINAL or state in ("prepared", "stored_pending")
    _exec(conn,
          "UPDATE audit SET state=?, outcome_code=?, latency_ms=?, normalized_ref=?,"
          " query_hmac=COALESCE(?, query_hmac), query_len=COALESCE(?, query_len)"
          " WHERE request_id=?",
          (state, outcome, latency_ms, normalized_ref, query_hmac, query_len, request_id),
          fp="audit.finalize")


def safe_query_hmac(audit_key: bytes, query: str) -> tuple[str, int]:
    return hmac.new(audit_key, query.encode("utf-8"), hashlib.sha256).hexdigest(), len(query)


def reconcile_startup(conn, pid: str) -> int:
    """Converge non-terminal rows left by other process instances (05 §5.3 / 07 §7.6)."""
    converged = 0
    rows = conn.execute(
        "SELECT request_id, event_id, state FROM audit"
        " WHERE state IN ('started','prepared','stored_pending') AND process_instance_id != ?",
        (pid,)).fetchall()
    for row in rows:
        if row["event_id"]:
            ev = conn.execute(
                "SELECT r.state AS rstate, o.status AS ostatus FROM idempotency_reservation r"
                " LEFT JOIN outbox o ON o.event_id = r.event_id WHERE r.event_id=?",
                (row["event_id"],)).fetchone()
            if ev and ev["ostatus"] in ("ready", "indexing", "retry_wait", "indexed"):
                conn.execute("UPDATE audit SET state='ok', outcome_code='COMMIT_STORED_INDEX_PENDING'"
                             " WHERE request_id=?", (row["request_id"],))
            elif ev and (ev["ostatus"] == "prepared" or ev["rstate"] in ("reserved", "retryable_failed")):
                conn.execute("UPDATE audit SET state='recovery_pending' WHERE request_id=?",
                             (row["request_id"],))
            else:
                conn.execute("UPDATE audit SET state='error', outcome_code=? WHERE request_id=?",
                             (OUTCOME_FINALIZE_LOST, row["request_id"]))
        else:
            conn.execute("UPDATE audit SET state='interrupted', outcome_code=? WHERE request_id=?",
                         (OUTCOME_FINALIZE_LOST, row["request_id"]))
        converged += 1
    return converged

"""Operation audit (plan 07 §7.6, F1): one row per request, pre-auth rows included."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time

from . import failpoints
from .constants import (
    AUDIT_RETENTION_BATCH_SIZE,
    AUDIT_RETENTION_DAYS,
    AUDIT_RETENTION_MAX_BATCHES,
    AUDIT_RETENTION_MAX_ROWS,
    AUDIT_RETENTION_RUN_BUDGET_S,
    AUDIT_TERMINAL,
    FIELD_PATHS,
    OUTCOME_FINALIZE_LOST,
    UNKNOWN_FIELD,
)
from .db import now


class AuditError(Exception):
    pass


_SAFE_SECRET_CATEGORIES = frozenset({
    "private_key", "token", "credential", "dsn", "cookie_header", "encoded_secret",
})
_UNKNOWN_CATEGORY = "unknown_category"
# recovery_pending is intentionally excluded: it is a truthful crash-recovery
# marker until the next startup has reconciled its artifact/outbox state.
_RETENTION_STATES = tuple(sorted(AUDIT_TERMINAL - {"recovery_pending"}))
_RETENTION_STATE_MARKS = ",".join("?" for _ in _RETENTION_STATES)


def _safe_enum_json(values, allowed, unknown: str) -> str | None:
    if values is None:
        return None
    normalized = sorted({value if isinstance(value, str) and value in allowed else unknown
                         for value in values})
    return json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))


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
             query_hmac: str | None = None, query_len: int | None = None,
             redacted_fields=None, redacted_categories=None) -> None:
    assert state in AUDIT_TERMINAL or state in ("prepared", "stored_pending")
    fields_json = _safe_enum_json(redacted_fields, frozenset(FIELD_PATHS), UNKNOWN_FIELD)
    categories_json = _safe_enum_json(
        redacted_categories, _SAFE_SECRET_CATEGORIES, _UNKNOWN_CATEGORY)
    _exec(conn,
          "UPDATE audit SET state=?, outcome_code=?, latency_ms=?, normalized_ref=?,"
          " query_hmac=COALESCE(?, query_hmac), query_len=COALESCE(?, query_len),"
          " redacted_fields=COALESCE(?, redacted_fields),"
          " redacted_categories=COALESCE(?, redacted_categories)"
          " WHERE request_id=?",
          (state, outcome, latency_ms, normalized_ref, query_hmac, query_len,
           fields_json, categories_json, request_id),
          fp="audit.finalize")


def safe_query_hmac(audit_key: bytes, query: str) -> tuple[str, int]:
    return hmac.new(audit_key, query.encode("utf-8"), hashlib.sha256).hexdigest(), len(query)


def purge_retention_batch(
        conn: sqlite3.Connection, *, cutoff_ts: int, max_rows: int = AUDIT_RETENTION_MAX_ROWS,
        batch_size: int = AUDIT_RETENTION_BATCH_SIZE) -> int:
    """Atomically delete at most one ordered batch of expired/excess audit rows.

    Audit rows are children of reservations, so this deletes only audit metadata;
    reservations and outbox work remain authoritative and untouched.
    """
    if max_rows < 0 or batch_size <= 0:
        raise ValueError("invalid audit retention bounds")
    if conn.in_transaction:
        raise AuditError("active transaction")

    try:
        failpoints.hit("audit.retention")
        conn.execute("BEGIN IMMEDIATE")
        expired = conn.execute(
            "DELETE FROM audit WHERE rowid IN ("
            f" SELECT rowid FROM audit WHERE state IN ({_RETENTION_STATE_MARKS})"
            " AND ts < ? ORDER BY ts, rowid LIMIT ?"
            ")",
            (*_RETENTION_STATES, cutoff_ts, batch_size),
        ).rowcount

        deleted = max(expired, 0)
        remaining_budget = batch_size - deleted
        if remaining_budget:
            row_count = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            excess = max(0, row_count - max_rows)
            if excess:
                capped = conn.execute(
                    "DELETE FROM audit WHERE rowid IN ("
                    f" SELECT rowid FROM audit WHERE state IN ({_RETENTION_STATE_MARKS})"
                    " ORDER BY ts, rowid LIMIT ?"
                    ")",
                    (*_RETENTION_STATES, min(excess, remaining_budget)),
                ).rowcount
                deleted += max(capped, 0)

        # Never meet the cap by deleting in-flight or unreconciled history.  If
        # protected rows alone exceed it, retain them and make readiness fail so
        # an operator sees that the configured bound cannot currently be met.
        protected = conn.execute(
            f"SELECT COUNT(*) FROM audit WHERE state NOT IN ({_RETENTION_STATE_MARKS})",
            _RETENTION_STATES,
        ).fetchone()[0]
        cap_blocked = protected > max_rows
        conn.execute("COMMIT")
        if cap_blocked:
            raise AuditError("audit retention cap blocked")
        return deleted
    except sqlite3.Error as exc:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise AuditError(exc.__class__.__name__) from exc
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def enforce_retention(
        conn: sqlite3.Connection, *, current_ts: int | None = None,
        max_rows: int = AUDIT_RETENTION_MAX_ROWS,
        batch_size: int = AUDIT_RETENTION_BATCH_SIZE,
        max_batches: int = AUDIT_RETENTION_MAX_BATCHES,
        run_budget_s: float = AUDIT_RETENTION_RUN_BUDGET_S) -> int:
    """Converge retention within an explicit batch and wall-clock budget.

    Each write transaction is bounded by ``batch_size``.  If the table cannot
    converge within this maintenance run, fail readiness instead of reporting a
    healthy but ineffective row cap.
    """
    if max_batches <= 0 or run_budget_s <= 0:
        raise ValueError("invalid audit retention run bounds")
    cutoff_ts = (now() if current_ts is None else current_ts) - AUDIT_RETENTION_DAYS * 86400
    deadline = time.monotonic() + run_budget_s
    total = 0
    for _ in range(max_batches):
        total += purge_retention_batch(
            conn, cutoff_ts=cutoff_ts, max_rows=max_rows, batch_size=batch_size)
        try:
            expired = conn.execute(
                f"SELECT 1 FROM audit WHERE state IN ({_RETENTION_STATE_MARKS})"
                " AND ts < ? LIMIT 1",
                (*_RETENTION_STATES, cutoff_ts),
            ).fetchone()
            row_count = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        except sqlite3.Error as exc:
            raise AuditError(exc.__class__.__name__) from exc
        if expired is None and row_count <= max_rows:
            return total
        if time.monotonic() >= deadline:
            break
    raise AuditError("audit retention maintenance budget exhausted")


def reconcile_startup(conn, pid: str) -> int:
    """Converge unfinished/recovery-pending rows from prior process instances."""
    converged = 0
    rows = conn.execute(
        "SELECT request_id, event_id, state FROM audit"
        " WHERE state IN ('started','prepared','stored_pending','recovery_pending')"
        " AND process_instance_id != ?",
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

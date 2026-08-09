"""Reservation/Outbox state machine with the F4 lease contract (plan 05)."""

from __future__ import annotations

import sqlite3

from .constants import (
    E_DURABILITY_GAP,
    OUTBOX_LEASE_TTL_S,
    RESERVATION_LEASE_TTL_S,
)
from .db import now


def begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def lookup_reservation(conn, client_id: str, key: str):
    return conn.execute(
        "SELECT * FROM idempotency_reservation WHERE client_id=? AND idempotency_key=?",
        (client_id, key)).fetchone()


def insert_reserved(conn, *, client_id: str, key: str, phmac: str, cv: str, event_id: str,
                    root_request_id: str, bank: str, document_id: str, uri: str,
                    doc_created_at: str, owner: str) -> None:
    t = now()
    conn.execute(
        "INSERT INTO idempotency_reservation (client_id, idempotency_key, payload_hmac,"
        " canonical_version, event_id, root_request_id, server_bank_id, document_id, uri,"
        " document_created_at, state, lease_owner, lease_expires_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?, 'reserved', ?, ?, ?, ?)",
        (client_id, key, phmac, cv, event_id, root_request_id, bank, document_id, uri,
         doc_created_at, owner, t + RESERVATION_LEASE_TTL_S, t, t))


def take_reservation_lease(conn, event_id: str, owner: str) -> bool:
    """CAS takeover: only free/expired leases can be claimed (F4)."""
    t = now()
    cur = conn.execute(
        "UPDATE idempotency_reservation SET lease_owner=?, lease_expires_at=?, updated_at=?"
        " WHERE event_id=? AND (lease_owner IS NULL OR lease_owner=? OR lease_expires_at < ?)",
        (owner, t + RESERVATION_LEASE_TTL_S, t, event_id, owner, t))
    return cur.rowcount == 1


def set_reservation_state(conn, event_id: str, state: str, *, release_lease: bool = False) -> None:
    if release_lease:
        conn.execute(
            "UPDATE idempotency_reservation SET state=?, lease_owner=NULL, lease_expires_at=NULL,"
            " updated_at=? WHERE event_id=?", (state, now(), event_id))
    else:
        conn.execute("UPDATE idempotency_reservation SET state=?, updated_at=? WHERE event_id=?",
                     (state, now(), event_id))


def insert_outbox_prepared(conn, res, desired_sha256: str, spv: str) -> None:
    t = now()
    conn.execute(
        "INSERT INTO outbox (event_id, root_request_id, client_id, idempotency_key,"
        " server_bank_id, document_id, uri, desired_sha256, scan_policy_version, status,"
        " created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?, 'prepared', ?, ?)",
        (res["event_id"], res["root_request_id"], res["client_id"], res["idempotency_key"],
         res["server_bank_id"], res["document_id"], res["uri"], desired_sha256, spv, t, t))


def set_outbox_status(conn, event_id: str, status: str, *, error_code: str | None = None,
                      next_attempt_at: int | None = None, release_lease: bool = False) -> None:
    if release_lease:
        conn.execute(
            "UPDATE outbox SET status=?, error_code=?, next_attempt_at=?,"
            " lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE event_id=?",
            (status, error_code, next_attempt_at, now(), event_id))
    else:
        conn.execute(
            "UPDATE outbox SET status=?, error_code=COALESCE(?, error_code),"
            " next_attempt_at=?, updated_at=? WHERE event_id=?",
            (status, error_code, next_attempt_at, now(), event_id))


def claim_next_event(conn, owner: str):
    """Worker claim: new work or an expired in-flight lease; the same atomic
    UPDATE moves the row to `indexing` and bumps attempt (05 §5.1/§5.4)."""
    t = now()
    eligible = (
        "(((status='ready' OR (status='retry_wait' AND next_attempt_at <= ?))"
        " AND (lease_owner IS NULL OR lease_expires_at < ?))"
        " OR (status='indexing' AND lease_expires_at < ?))"
    )
    begin_immediate(conn)
    try:
        row = conn.execute(
            f"SELECT event_id FROM outbox WHERE {eligible}"
            " ORDER BY created_at LIMIT 1", (t, t, t)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        cur = conn.execute(
            "UPDATE outbox SET status='indexing', attempt=attempt+1, lease_owner=?,"
            f" lease_expires_at=?, updated_at=? WHERE event_id=? AND {eligible}",
            (owner, t + OUTBOX_LEASE_TTL_S, t, row["event_id"], t, t, t))
        if cur.rowcount != 1:
            conn.execute("COMMIT")
            return None
        claimed = conn.execute("SELECT * FROM outbox WHERE event_id=?", (row["event_id"],)).fetchone()
        conn.execute("COMMIT")
        return claimed
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def mark_conflict(conn, event_id: str, error_code: str) -> None:
    """Atomically mark both halves of an event conflicted and release leases."""
    begin_immediate(conn)
    try:
        set_outbox_status(conn, event_id, "conflict", error_code=error_code, release_lease=True)
        set_reservation_state(conn, event_id, "conflict", release_lease=True)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def mark_durability_gap(conn, event_id: str) -> None:
    """prepared with both staging and final file missing: both rows -> conflict (F8)."""
    mark_conflict(conn, event_id, E_DURABILITY_GAP)


def status_by_ref(conn, *, document_id: str | None = None, client_id: str | None = None,
                  idempotency_key: str | None = None):
    if document_id is not None:
        res = conn.execute("SELECT * FROM idempotency_reservation WHERE document_id=?",
                           (document_id,)).fetchone()
    else:
        res = lookup_reservation(conn, client_id, idempotency_key)
    if res is None:
        return None, None
    out = conn.execute("SELECT * FROM outbox WHERE event_id=?", (res["event_id"],)).fetchone()
    return res, out

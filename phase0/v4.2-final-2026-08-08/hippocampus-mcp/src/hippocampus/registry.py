"""Client registry + unified authorization + union source gate (plan 04 §4.5)."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import sqlite3

from .db import now


class AuthzError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def token_hash(pepper: bytes, token: str) -> str:
    return hmac.new(pepper, token.encode("utf-8"), hashlib.sha256).hexdigest()


def create_client(conn: sqlite3.Connection, client_id: str, token: str, pepper: bytes, *,
                  source_tag: str, readable: list[str], writable: list[str],
                  types: list[str], expires_at: int | None = None,
                  disabled: bool = False) -> None:
    conn.execute(
        "INSERT INTO clients (client_id, token_hash, source_tag, readable_projects,"
        " writable_projects, allowed_types, expires_at, revoked_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (client_id, token_hash(pepper, token), source_tag, json.dumps(readable),
         json.dumps(writable), json.dumps(types), expires_at,
         now() if disabled else None, now()),
    )


def rotate_token(conn: sqlite3.Connection, client_id: str, token: str, pepper: bytes) -> None:
    cur = conn.execute(
        "UPDATE clients SET token_hash=?, token_version=token_version+1, revoked_at=NULL"
        " WHERE client_id=?", (token_hash(pepper, token), client_id))
    if cur.rowcount != 1:
        raise AuthzError("NO_SUCH_CLIENT")


def revoke_client(conn: sqlite3.Connection, client_id: str) -> None:
    conn.execute("UPDATE clients SET revoked_at=? WHERE client_id=?", (now(), client_id))


def set_grants(conn: sqlite3.Connection, client_id: str, *, readable: list[str],
               writable: list[str]) -> None:
    cur = conn.execute(
        "UPDATE clients SET readable_projects=?, writable_projects=? WHERE client_id=?",
        (json.dumps(readable), json.dumps(writable), client_id))
    if cur.rowcount != 1:
        raise AuthzError("NO_SUCH_CLIENT")


def bind_peer(conn: sqlite3.Connection, client_id: str, ip: str) -> None:
    addr = ipaddress.ip_address(ip)  # exact single address only; raises on CIDR/hostname
    conn.execute(
        "INSERT INTO client_sources (client_id, canonical_ip, address_family, verified_at)"
        " VALUES (?,?,?,?)"
        " ON CONFLICT(client_id, canonical_ip) DO UPDATE SET revoked_at=NULL, verified_at=excluded.verified_at",
        (client_id, str(addr), f"ipv{addr.version}", now()),
    )


def revoke_peer(conn: sqlite3.Connection, client_id: str, ip: str) -> None:
    addr = ipaddress.ip_address(ip)  # keep revoke spelling-equivalent to bind_peer
    cur = conn.execute(
        "UPDATE client_sources SET revoked_at=? WHERE client_id=? AND canonical_ip=?",
        (now(), client_id, str(addr)),
    )
    if cur.rowcount != 1:
        raise AuthzError("NO_SUCH_PEER")


def _active_clause() -> str:
    return "revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)"


def union_gate(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute(
        f"SELECT DISTINCT s.canonical_ip FROM client_sources s"
        f" JOIN clients c ON c.client_id = s.client_id"
        f" WHERE s.revoked_at IS NULL AND c.{_active_clause()}", (now(),)).fetchall()
    return frozenset(r["canonical_ip"] for r in rows)


def find_client_by_token(conn: sqlite3.Connection, token: str, pepper: bytes):
    presented = token_hash(pepper, token)
    match = None
    for row in conn.execute(f"SELECT * FROM clients WHERE {_active_clause()}", (now(),)):
        if hmac.compare_digest(row["token_hash"], presented):
            match = row  # constant-shape loop: no early exit on match
    return match


def peer_bound(conn: sqlite3.Connection, client_id: str, peer_ip: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM client_sources WHERE client_id=? AND canonical_ip=? AND revoked_at IS NULL",
        (client_id, peer_ip)).fetchone()
    return row is not None


def authorize(client_row, *, action: str, project: str, type_: str | None = None) -> None:
    """Shared authorization for all four tools (04 §4.5)."""
    readable = set(json.loads(client_row["readable_projects"]))
    writable = set(json.loads(client_row["writable_projects"]))
    allowed_types = set(json.loads(client_row["allowed_types"]))
    if action == "write":
        if project not in writable:
            raise AuthzError("PROJECT_NOT_WRITABLE")
    elif project not in readable:
        raise AuthzError("PROJECT_NOT_READABLE")
    if type_ is not None and type_ not in allowed_types:
        raise AuthzError("TYPE_NOT_ALLOWED")


def list_safe(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for row in conn.execute("SELECT * FROM clients ORDER BY client_id"):
        sources = [r["canonical_ip"] for r in conn.execute(
            "SELECT canonical_ip FROM client_sources WHERE client_id=? AND revoked_at IS NULL",
            (row["client_id"],))]
        out.append({
            "client_id": row["client_id"],
            "token_version": row["token_version"],
            "source_tag": row["source_tag"],
            "readable_projects": json.loads(row["readable_projects"]),
            "writable_projects": json.loads(row["writable_projects"]),
            "allowed_types": json.loads(row["allowed_types"]),
            "expires_at": row["expires_at"],
            "revoked": row["revoked_at"] is not None,
            "sources": sources,
        })
    return out

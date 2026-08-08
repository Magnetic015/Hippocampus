"""SQLite state store (plan 05 §5.1, 04 §4.5, 07 §7.6). One file, four tables."""

from __future__ import annotations

import os
import sqlite3
import time

_DDL = """
CREATE TABLE IF NOT EXISTS clients (
  client_id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL,
  token_version INTEGER NOT NULL DEFAULT 1,
  source_tag TEXT NOT NULL,
  permissions TEXT NOT NULL DEFAULT 'rw',
  readable_projects TEXT NOT NULL,
  writable_projects TEXT NOT NULL,
  allowed_types TEXT NOT NULL,
  expires_at INTEGER,
  revoked_at INTEGER,
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER
);
CREATE TABLE IF NOT EXISTS client_sources (
  client_id TEXT NOT NULL REFERENCES clients(client_id),
  canonical_ip TEXT NOT NULL,
  address_family TEXT NOT NULL,
  verified_at INTEGER NOT NULL,
  revoked_at INTEGER,
  PRIMARY KEY (client_id, canonical_ip)
);
CREATE TABLE IF NOT EXISTS idempotency_reservation (
  client_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_hmac TEXT NOT NULL,
  canonical_version TEXT NOT NULL,
  event_id TEXT NOT NULL UNIQUE,
  root_request_id TEXT NOT NULL UNIQUE,
  server_bank_id TEXT NOT NULL,
  document_id TEXT NOT NULL UNIQUE,
  uri TEXT NOT NULL UNIQUE,
  document_created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('reserved','retryable_failed','prepared','stored','rejected','conflict')),
  lease_owner TEXT,
  lease_expires_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (client_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS outbox (
  event_id TEXT PRIMARY KEY REFERENCES idempotency_reservation(event_id),
  root_request_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  server_bank_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  uri TEXT NOT NULL,
  event_type TEXT NOT NULL DEFAULT 'upsert',
  desired_sha256 TEXT NOT NULL,
  previous_sha256 TEXT,
  scan_policy_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('prepared','ready','indexing','retry_wait','indexed','dead','policy_blocked','conflict')),
  attempt INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER,
  lease_owner TEXT,
  lease_expires_at INTEGER,
  error_code TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (document_id, desired_sha256, event_type)
);
CREATE TABLE IF NOT EXISTS audit (
  request_id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  process_instance_id TEXT NOT NULL,
  root_request_id TEXT,
  event_id TEXT REFERENCES idempotency_reservation(event_id),
  client_id TEXT,
  source_ip TEXT,
  operation_enum TEXT,
  tool_enum TEXT,
  normalized_ref TEXT,
  state TEXT NOT NULL CHECK (state IN
    ('started','prepared','stored_pending','ok','idempotent_replay',
     'rejected','error','interrupted','recovery_pending')),
  outcome_code TEXT,
  query_hmac TEXT,
  query_len INTEGER,
  latency_ms INTEGER,
  scan_policy_version TEXT
);
"""


def now() -> int:
    return int(time.time())


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def init_db(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    prior = os.path.exists(path)
    conn = connect(path)
    try:
        conn.executescript(_DDL)
    finally:
        conn.close()
    if not prior:
        os.chmod(path, 0o600)

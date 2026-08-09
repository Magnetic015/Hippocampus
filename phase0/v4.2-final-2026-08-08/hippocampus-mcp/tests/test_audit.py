"""09 §10.7: exactly one audit row per request, audit-before-response, safe fields."""

import json
import sqlite3

import pytest
from harness import CLIENTS, commit_args

from hippocampus import audit, constants as C, failpoints


@pytest.fixture(autouse=True)
def _clear():
    failpoints.clear()
    yield
    failpoints.clear()


def test_one_row_per_authenticated_request(lab, key):
    lab.rpc("initialize")
    lab.rpc("tools/list")
    lab.call("memory_commit", commit_args(key()))
    lab.call("memory_search", {"query": "写入链路", "project": "commissioning"})
    rows = lab.audit_rows()
    assert len(rows) == 4
    assert len({r["request_id"] for r in rows}) == 4
    ops = [r["operation_enum"] for r in rows]
    assert ops == ["initialize", "tools_list", "tools_call", "tools_call"]
    assert [r["tool_enum"] for r in rows] == [None, None, "memory_commit", "memory_search"]
    assert all(r["state"] in ("ok",) for r in rows)
    assert all(r["scan_policy_version"] == "sp1" for r in rows if r["client_id"])


def test_duplicate_request_id_is_impossible(lab):
    conn = lab.db()
    try:
        audit.insert_started(conn, "fixed-id", "pid", "mac-claude", "sp1")
        try:
            audit.insert_started(conn, "fixed-id", "pid", "mac-claude", "sp1")
            raise AssertionError("duplicate request_id accepted")
        except audit.AuditError:
            pass
    finally:
        conn.close()


def test_commit_row_links_outbox_via_root_and_event(lab, key):
    _, data, _ = lab.call("memory_commit", commit_args(key()))
    conn = lab.db()
    try:
        joined = conn.execute(
            "SELECT a.request_id, a.root_request_id, a.event_id, o.status, o.document_id"
            " FROM audit a JOIN outbox o ON o.event_id = a.event_id").fetchall()
    finally:
        conn.close()
    assert len(joined) == 1
    assert joined[0]["document_id"] == data["document_id"]
    assert joined[0]["root_request_id"] == joined[0]["request_id"]  # first commit


def test_audit_never_contains_payload_or_headers(lab, key):
    marker = "这句只出现在正文里"
    lab.call("memory_commit", commit_args(key(), detail_body=marker, title="敏感标题占位"))
    lab.call("memory_search", {"query": "特定查询词", "project": "commissioning"})
    blob = json.dumps(lab.audit_rows(), ensure_ascii=False)
    for needle in (marker, "敏感标题占位", "特定查询词", CLIENTS["mac-claude"],
                   "Authorization", "Bearer", "tools/call"):
        assert needle not in blob, needle


def test_audit_insert_failure_blocks_everything(lab_noworker, key, monkeypatch):
    lab = lab_noworker
    failpoints.arm("audit.insert", sqlite3.OperationalError("disk I/O error"))
    status, data, is_error = lab.call("memory_commit", commit_args(key()))
    assert status == 503 and data["code"] == C.E_AUDIT_UNAVAILABLE
    conn = lab.db()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM idempotency_reservation").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"] == 0
    finally:
        conn.close()
    from pathlib import Path
    assert list(Path(lab.vault).rglob("*.md")) == []
    assert lab.mock.retain_log == []


def test_search_result_discarded_when_final_audit_fails(lab, key):
    lab.call("memory_commit", commit_args(key()))
    assert lab.drain_worker()
    failpoints.arm("audit.finalize", sqlite3.OperationalError("disk I/O error"))
    _, data, is_error = lab.call("memory_search",
                                 {"query": "Hippocampus 写入链路", "project": "commissioning"})
    assert is_error and data["code"] == C.E_AUDIT_UNAVAILABLE
    assert "results" not in data  # results discarded, not returned


def test_commit_reports_recovery_pending_when_final_audit_fails(lab_noworker, key):
    lab = lab_noworker
    failpoints.arm("audit.finalize", sqlite3.OperationalError("disk I/O error"))
    _, data, is_error = lab.call("memory_commit", commit_args(key()))
    assert not is_error
    assert data["accepted"] and data["stored"]
    assert data["index_state"] == "recovery_pending"  # truthful, never a plain success
    conn = lab.db()
    try:
        row = conn.execute("SELECT state FROM audit WHERE event_id IS NOT NULL").fetchone()
    finally:
        conn.close()
    assert row["state"] == "stored_pending"  # converged later by startup recovery


def test_rejections_have_exactly_one_redacted_row(lab, key):
    cases = [
        ("memory_commit", commit_args("not-a-uuid")),
        ("memory_commit", commit_args(key(), project="global")),
        ("memory_commit", commit_args(key(), type="nope")),
        ("memory_search", {"query": "x", "project": "commissioning", "bogus": 1}),
    ]
    for tool, args in cases:
        lab.call(tool, args)
    rows = lab.audit_rows()
    assert len(rows) == len(cases)
    assert all(r["state"] == "rejected" for r in rows)
    assert all(r["tool_enum"] in C.TOOLS for r in rows)
    blob = json.dumps(rows, ensure_ascii=False)
    assert "bogus" not in blob and "not-a-uuid" not in blob


def test_secret_rejection_records_categories_and_paths_only(lab, key):
    _, data, is_error = lab.call(
        "memory_commit", commit_args(key(), detail_body="AKIACANARY0EXAMPLE99"))
    assert is_error
    assert data["fields"] == ["detail_body"]
    assert data["categories"] == ["credential"]
    row = lab.audit_rows()[0]
    assert row["state"] == "rejected" and row["outcome_code"] == C.OUTCOME_SECRET_REJECTED
    assert json.loads(row["redacted_fields"]) == ["detail_body"]
    assert json.loads(row["redacted_categories"]) == ["credential"]
    assert "AKIA" not in json.dumps(dict(row), ensure_ascii=False)


def test_init_db_migrates_existing_audit_table(tmp_path):
    from hippocampus.db import connect, init_db

    db_path = tmp_path / "state" / "outbox.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE audit (request_id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    init_db(str(db_path))
    conn = connect(str(db_path))
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit)")}
    finally:
        conn.close()
    assert {"redacted_fields", "redacted_categories"} <= columns


def test_latency_recorded(lab):
    lab.rpc("tools/list")
    assert lab.audit_rows()[0]["latency_ms"] is not None


def test_audit_retention_delete_does_not_cascade(lab, key):
    _, data, _ = lab.call("memory_commit", commit_args(key()))
    conn = lab.db()
    try:
        current_ts = 2_000_000_000
        conn.execute(
            "UPDATE audit SET ts=?",
            (current_ts - C.AUDIT_RETENTION_DAYS * 86400 - 1,),
        )
        assert audit.enforce_retention(conn, current_ts=current_ts) == 1
        res = conn.execute("SELECT root_request_id, event_id FROM idempotency_reservation").fetchone()
        out = conn.execute("SELECT event_id, root_request_id FROM outbox").fetchone()
    finally:
        conn.close()
    assert res is not None and out is not None  # reservation/outbox survive intact
    assert res["root_request_id"] and res["event_id"] == out["event_id"]
    _, status_data, is_error = lab.call("memory_status", {"document_id": data["document_id"]})
    assert not is_error

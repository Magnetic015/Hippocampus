"""Regression coverage for the code-level findings from PR #1 review."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from harness import CLIENTS, commit_args

from hippocampus import constants as C, failpoints, tools, vault
from hippocampus.recovery import run_startup_recovery
from hippocampus.worker import Worker


@pytest.fixture(autouse=True)
def _clear_failpoints():
    failpoints.clear()
    yield
    failpoints.clear()


def _commit(lab, key, **over):
    _, data, is_error = lab.call("memory_commit", commit_args(key(), **over))
    assert not is_error, data
    return data


def _indexed_item(lab, key):
    data = _commit(lab, key)
    assert Worker(lab.env).process_one()
    return data, lab.mock.banks[lab.env.bank][data["document_id"]]


def _one_fact(data, item):
    return {
        "results": [{
            "document_id": data["document_id"],
            "text": item["content"],
            "scores": {"final": 1.0},
            "metadata": item["metadata"],
            "tags": item["tags"],
        }],
    }


def test_read_rescans_on_policy_upgrade_and_records_current_version(lab_noworker, key):
    lab = lab_noworker
    blocked = _commit(lab, key, title="新策略命中标记")
    clean = _commit(lab, key)
    lab.env.spv = "sp2"
    scanned = []

    def upgraded_scan(text):
        if "新策略命中标记" in text:
            scanned.append(text)
            return {"new-rule"}
        return set()

    lab.env.scan_text = upgraded_scan
    _, error, is_error = lab.call("memory_read", {"uris": [blocked["uri"]]})
    assert is_error and error["code"] == C.E_POLICY_BLOCKED
    assert scanned

    _, result, is_error = lab.call("memory_read", {"uris": [clean["uri"]]})
    assert not is_error and result["documents"][0]["uri"] == clean["uri"]
    conn = lab.db()
    try:
        rows = {row["document_id"]: dict(row) for row in conn.execute(
            "SELECT document_id, status, scan_policy_version FROM outbox")}
    finally:
        conn.close()
    assert rows[blocked["document_id"]] == {
        "document_id": blocked["document_id"],
        "status": "policy_blocked",
        "scan_policy_version": "sp2",
    }
    assert rows[clean["document_id"]]["scan_policy_version"] == "sp2"


def test_search_rechecks_optional_type_and_source_filters(lab_noworker, key):
    lab = lab_noworker
    data, item = _indexed_item(lab, key)
    lab.env.hindsight.recall = lambda *args, **kwargs: _one_fact(data, item)

    _, result, is_error = lab.call("memory_search", {
        "query": "Hippocampus",
        "project": "commissioning",
        "type": "fact",
        "source": "mac-codex",
    })
    assert not is_error and result["results"] == []


def test_search_scans_backend_source_and_tags_before_return(lab_noworker, key):
    lab = lab_noworker
    data, item = _indexed_item(lab, key)
    marker = "LEAK-MARKER"
    item["metadata"] = dict(item["metadata"], source_agent=marker)
    item["tags"] = [marker if tag == "src:mac-claude" else tag for tag in item["tags"]]
    item["tags"] = [f"src:{marker}" if tag == marker else tag for tag in item["tags"]]
    conn = lab.db()
    try:
        conn.execute("UPDATE clients SET source_tag=? WHERE client_id='mac-claude'", (marker,))
    finally:
        conn.close()
    seen = []

    def output_scan(text):
        if marker in text:
            seen.append(text)
            return {"credential"}
        return set()

    lab.env.scan_text = output_scan
    _, result, is_error = lab.call(
        "memory_search", {"query": "Hippocampus", "project": "commissioning"})
    assert not is_error and result["results"] == []
    assert seen


def test_search_secret_rejection_preserves_safe_scanner_category(lab_noworker):
    lab = lab_noworker
    canary = "AKIACANARY0EXAMPLE99"
    recall_calls = []

    def unexpected_recall(*args, **kwargs):
        recall_calls.append((args, kwargs))
        return {"results": []}

    lab.env.hindsight.recall = unexpected_recall
    _, error, is_error = lab.call(
        "memory_search", {"query": canary, "project": "commissioning"})
    assert is_error and error["code"] == C.E_SECRET_REJECTED
    assert error["fields"] == ["query"]
    assert error["categories"] == ["credential"]
    assert recall_calls == []

    rows = lab.audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome_code"] == C.OUTCOME_SECRET_REJECTED
    assert json.loads(row["redacted_fields"]) == ["query"]
    assert json.loads(row["redacted_categories"]) == ["credential"]
    assert canary not in json.dumps(error, ensure_ascii=False)
    assert canary not in json.dumps(dict(row), ensure_ascii=False)


def test_recall_failure_and_success_update_readyz_degradation(lab_noworker):
    lab = lab_noworker
    lab.mock.fail_next(1)
    _, error, is_error = lab.call(
        "memory_search", {"query": "Hippocampus", "project": "commissioning"})
    assert is_error and error["code"] == C.E_INDEX_UNAVAILABLE
    status, body = lab.raw(b"", method="GET", url=lab.url.replace("/mcp/", "/readyz"))
    assert status == 200 and body["status"] == "degraded"

    _, result, is_error = lab.call(
        "memory_search", {"query": "Hippocampus", "project": "commissioning"})
    assert not is_error and result["results"] == []
    status, body = lab.raw(b"", method="GET", url=lab.url.replace("/mcp/", "/readyz"))
    assert status == 200 and body["status"] == "ready"


def test_read_rejects_clean_hash_mismatch_and_marks_conflict(lab_noworker, key):
    lab = lab_noworker
    data = _commit(lab, key)
    project, document_id = vault.parse_uri(data["uri"])
    path = vault.doc_path(lab.vault, project, document_id)
    path.write_text("clean external replacement", encoding="utf-8")

    _, error, is_error = lab.call("memory_read", {"uris": [data["uri"]]})
    assert is_error and error["code"] == C.E_STATE_INVARIANT
    conn = lab.db()
    try:
        row = dict(conn.execute(
            "SELECT r.state AS reservation_state, o.status, o.error_code"
            " FROM idempotency_reservation r JOIN outbox o USING(event_id)").fetchone())
    finally:
        conn.close()
    assert row == {
        "reservation_state": "conflict",
        "status": "conflict",
        "error_code": C.E_HASH_MISMATCH,
    }


def test_read_rejects_symlinked_artifact_and_marks_conflict(lab_noworker, key):
    lab = lab_noworker
    data = _commit(lab, key)
    project, document_id = vault.parse_uri(data["uri"])
    path = vault.doc_path(lab.vault, project, document_id)
    target = Path(lab.tmp) / "read-symlink-target"
    target.write_bytes(path.read_bytes())
    target.chmod(0o600)
    path.unlink()
    path.symlink_to(target)

    _, error, is_error = lab.call("memory_read", {"uris": [data["uri"]]})

    assert is_error and error["code"] == C.E_STATE_INVARIANT
    conn = lab.db()
    try:
        row = dict(conn.execute(
            "SELECT r.state AS reservation_state, o.status, o.error_code"
            " FROM idempotency_reservation r JOIN outbox o USING(event_id)"
        ).fetchone())
    finally:
        conn.close()
    assert row == {
        "reservation_state": "conflict",
        "status": "conflict",
        "error_code": C.E_HASH_MISMATCH,
    }


def test_post_prepare_failure_preserves_recoverable_artifacts(lab_noworker, key):
    lab = lab_noworker
    idempotency_key = key()
    failpoints.arm("commit.prepared.after")
    _, result, is_error = lab.call("memory_commit", commit_args(idempotency_key))
    assert not is_error and result["index_state"] == "recovery_pending"

    conn = lab.db()
    try:
        row = dict(conn.execute(
            "SELECT r.state AS reservation_state, o.status, r.event_id, r.uri"
            " FROM idempotency_reservation r JOIN outbox o USING(event_id)").fetchone())
    finally:
        conn.close()
    project, document_id = vault.parse_uri(row["uri"])
    staging = vault.staging_path(lab.vault, project, row["event_id"])
    final = vault.doc_path(lab.vault, project, document_id)
    assert row["reservation_state"] == row["status"] == "prepared"
    assert staging.exists() and not final.exists()

    _, replay, is_error = lab.call("memory_commit", commit_args(idempotency_key))
    assert not is_error and replay["index_state"] == "recovery_pending"
    assert staging.exists() and not final.exists()
    stats = run_startup_recovery(lab.env)
    assert stats["promoted"] == 1 and final.exists() and not staging.exists()


def test_retry_rejects_unvalidated_existing_staging(lab_noworker, key):
    lab = lab_noworker
    idempotency_key = key()
    failpoints.arm("commit.staging.before")
    _, _, is_error = lab.call("memory_commit", commit_args(idempotency_key))
    assert is_error
    conn = lab.db()
    try:
        res = dict(conn.execute("SELECT * FROM idempotency_reservation").fetchone())
    finally:
        conn.close()
    project, document_id = vault.parse_uri(res["uri"])
    staging = vault.staging_path(lab.vault, project, res["event_id"])
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text("unvalidated old staging", encoding="utf-8")
    staging.chmod(0o644)

    _, error, is_error = lab.call("memory_commit", commit_args(idempotency_key))
    assert is_error and error["code"] == C.E_HASH_MISMATCH
    assert staging.exists()
    assert not vault.doc_path(lab.vault, project, document_id).exists()
    conn = lab.db()
    try:
        assert conn.execute("SELECT state FROM idempotency_reservation").fetchone()["state"] == "conflict"
        assert conn.execute("SELECT COUNT(*) AS count FROM outbox").fetchone()["count"] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("failure_kind", "expected_code"),
    [
        ("missing", C.E_DURABILITY_GAP),
        ("dangling_symlink", C.E_HASH_MISMATCH),
    ],
)
def test_commit_never_marks_missing_or_unsafe_staging_stored(
        lab_noworker, key, monkeypatch, failure_kind, expected_code):
    lab = lab_noworker
    original_hit = failpoints.hit

    def sabotage_staging(name):
        if name != "commit.rename.before":
            return original_hit(name)
        staging = next(Path(lab.vault).rglob(".hippocampus-*.staging"))
        staging.unlink()
        if failure_kind == "dangling_symlink":
            staging.symlink_to(Path(lab.tmp) / "missing-staging-target")

    monkeypatch.setattr(failpoints, "hit", sabotage_staging)
    _, error, is_error = lab.call("memory_commit", commit_args(key()))
    assert is_error and error["code"] == expected_code

    conn = lab.db()
    try:
        row = dict(conn.execute(
            "SELECT r.state AS reservation_state, o.status, o.error_code, r.uri"
            " FROM idempotency_reservation r JOIN outbox o USING(event_id)"
        ).fetchone())
    finally:
        conn.close()
    assert row["reservation_state"] == row["status"] == "conflict"
    assert row["error_code"] == expected_code
    project, document_id = vault.parse_uri(row["uri"])
    assert not vault.doc_path(lab.vault, project, document_id).exists()


def test_valid_final_does_not_mask_unsafe_staging(lab_noworker, key, monkeypatch):
    lab = lab_noworker
    original_hit = failpoints.hit

    def restore_final_and_tamper_staging(name):
        if name != "commit.rename.before":
            return original_hit(name)
        staging = next(Path(lab.vault).rglob(".hippocampus-*.staging"))
        desired = staging.read_bytes()
        conn = lab.db()
        try:
            uri = conn.execute("SELECT uri FROM idempotency_reservation").fetchone()["uri"]
        finally:
            conn.close()
        project, document_id = vault.parse_uri(uri)
        final = vault.doc_path(lab.vault, project, document_id)
        final.write_bytes(desired)
        final.chmod(0o600)
        staging.unlink()
        staging.symlink_to(Path(lab.tmp) / "missing-staging-target")

    monkeypatch.setattr(failpoints, "hit", restore_final_and_tamper_staging)
    _, error, is_error = lab.call("memory_commit", commit_args(key()))
    assert is_error and error["code"] == C.E_HASH_MISMATCH
    conn = lab.db()
    try:
        row = dict(conn.execute(
            "SELECT r.state AS reservation_state, o.status, o.error_code"
            " FROM idempotency_reservation r JOIN outbox o USING(event_id)"
        ).fetchone())
    finally:
        conn.close()
    assert row == {
        "reservation_state": "conflict",
        "status": "conflict",
        "error_code": C.E_HASH_MISMATCH,
    }


def test_search_requires_authoritative_local_provenance_and_indexed_state(
        lab_noworker, key):
    lab = lab_noworker
    data, _ = _indexed_item(lab, key)
    original_uri = data["uri"]
    other_uri = original_uri.replace("/commissioning/", "/global/")
    conn = lab.db()
    try:
        conn.execute("UPDATE idempotency_reservation SET uri=?", (other_uri,))
        conn.execute("UPDATE outbox SET uri=?", (other_uri,))
    finally:
        conn.close()
    _, result, is_error = lab.call(
        "memory_search", {"query": "Hippocampus", "project": "commissioning"})
    assert not is_error and result["results"] == []

    conn = lab.db()
    try:
        conn.execute("UPDATE idempotency_reservation SET uri=?", (original_uri,))
        conn.execute("UPDATE outbox SET uri=?, status='dead'", (original_uri,))
    finally:
        conn.close()
    _, result, is_error = lab.call(
        "memory_search", {"query": "Hippocampus", "project": "commissioning"})
    assert not is_error and result["results"] == []

    conn = lab.db()
    try:
        conn.execute("UPDATE outbox SET status='indexed', server_bank_id='other-bank'")
        conn.execute("UPDATE idempotency_reservation SET server_bank_id='other-bank'")
    finally:
        conn.close()
    _, result, is_error = lab.call(
        "memory_search", {"query": "Hippocampus", "project": "commissioning"})
    assert not is_error and result["results"] == []


def test_search_binds_recalled_source_to_reservation_client(lab_noworker, key):
    lab = lab_noworker
    _, item = _indexed_item(lab, key)
    item["metadata"] = dict(item["metadata"], source_agent="mac-codex")
    item["tags"] = [
        "src:mac-codex" if tag == "src:mac-claude" else tag for tag in item["tags"]
    ]

    _, result, is_error = lab.call(
        "memory_search", {"query": "Hippocampus", "project": "commissioning"})
    assert not is_error and result["results"] == []


def test_read_requires_requested_uri_to_match_local_provenance(lab_noworker, key):
    lab = lab_noworker
    data = _commit(lab, key)
    authoritative_uri = data["uri"].replace("/commissioning/", "/global/")
    conn = lab.db()
    try:
        conn.execute("UPDATE idempotency_reservation SET uri=?", (authoritative_uri,))
        conn.execute("UPDATE outbox SET uri=?", (authoritative_uri,))
    finally:
        conn.close()

    _, direct, is_error = lab.call("memory_read", {"uris": [authoritative_uri]})
    assert is_error and direct["code"] == C.E_AUTHZ_DENIED
    _, copied, is_error = lab.call("memory_read", {"uris": [data["uri"]]})
    assert is_error and copied["code"] == C.E_NOT_FOUND
    project, document_id = vault.parse_uri(data["uri"])
    assert Path(vault.doc_path(lab.vault, project, document_id)).exists()


def test_quiet_finalize_keeps_audit_pending_when_sink_fails(lab_noworker):
    ctx = SimpleNamespace(request_id="missing-request", audit_done=False)
    failpoints.arm("audit.finalize", sqlite3.OperationalError("disk I/O error"))
    conn = lab_noworker.db()
    try:
        assert tools._finalize_quiet(conn, ctx, "error", "retryable_failed") is False
    finally:
        conn.close()
    assert ctx.audit_done is False


def test_request_bearer_is_rejected_from_commit_and_search_business_values(
        lab_noworker, key):
    lab = lab_noworker
    token = CLIENTS["mac-claude"]

    status, response = lab.rpc("tools/list")
    assert status == 200 and "result" in response  # Authorization itself remains valid.

    rejected_fields = []
    for field, leaked_value in (
            ("title", token),
            ("detail_body", f"prefix-{token}-suffix")):
        _, error, is_error = lab.call(
            "memory_commit", commit_args(key(), **{field: leaked_value}))
        assert is_error and error["code"] == C.E_SECRET_REJECTED
        assert error["fields"] == [field] and error["categories"] == ["token"]
        assert token not in json.dumps(error, ensure_ascii=False)
        rejected_fields.append(field)

    recall_calls = []

    def unexpected_recall(*args, **kwargs):
        recall_calls.append((args, kwargs))
        return {"results": []}

    lab.env.hindsight.recall = unexpected_recall
    _, error, is_error = lab.call("memory_search", {
        "query": f"safe-prefix-{token}-safe-suffix",
        "project": "commissioning",
    })
    assert is_error and error["code"] == C.E_SECRET_REJECTED
    assert error["fields"] == ["query"] and error["categories"] == ["token"]
    assert recall_calls == []
    rejected_fields.append("query")

    failpoints.arm("audit.finalize", sqlite3.OperationalError("disk I/O error"))
    _, error, is_error = lab.call(
        "memory_commit", commit_args(key(), title=f"again-{token}"))
    assert is_error and error["code"] == C.E_AUDIT_UNAVAILABLE

    assert list(Path(lab.vault).rglob("*.md")) == []
    assert list(Path(lab.vault).rglob(".hippocampus-*.staging")) == []
    assert lab.mock.retain_log == []
    conn = lab.db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM idempotency_reservation").fetchone()["count"] == 0
        assert conn.execute("SELECT COUNT(*) AS count FROM outbox").fetchone()["count"] == 0
    finally:
        conn.close()

    rows = lab.audit_rows()
    recorded = [row for row in rows if row["outcome_code"] == C.OUTCOME_SECRET_REJECTED]
    assert [json.loads(row["redacted_fields"])[0] for row in recorded] == rejected_fields
    assert all(json.loads(row["redacted_categories"]) == ["token"] for row in recorded)
    assert token not in json.dumps(rows, ensure_ascii=False)


def test_request_bearer_in_rpc_id_or_tool_name_is_never_echoed(lab_noworker):
    lab = lab_noworker
    token = CLIENTS["mac-claude"]
    responses = []

    status, response = lab.rpc("tools/list", rpc_id=token)
    assert status == 200 and response["id"] is None
    error = json.loads(response["result"]["content"][0]["text"])
    assert error["code"] == C.E_SECRET_REJECTED
    assert error["fields"] == ["jsonrpc_id"] and error["categories"] == ["token"]
    responses.append(response)

    status, response = lab.rpc(
        "tools/call",
        {"name": f"prefix-{token}-suffix", "arguments": {}},
        rpc_id="safe-id",
    )
    assert status == 200 and response["id"] is None
    error = json.loads(response["result"]["content"][0]["text"])
    assert error["code"] == C.E_SECRET_REJECTED
    assert error["fields"] == ["jsonrpc_tool_name"]
    responses.append(response)

    failpoints.arm("audit.finalize", sqlite3.OperationalError("disk I/O error"))
    status, response = lab.rpc("tools/list", rpc_id=f"again-{token}")
    assert status == 200 and response["id"] is None
    error = json.loads(response["result"]["content"][0]["text"])
    assert error["code"] == C.E_AUDIT_UNAVAILABLE
    responses.append(response)

    rows = lab.audit_rows()
    recorded = [row for row in rows if row["outcome_code"] == C.OUTCOME_SECRET_REJECTED]
    assert [json.loads(row["redacted_fields"])[0] for row in recorded] == [
        "jsonrpc_id", "jsonrpc_tool_name",
    ]
    assert all(json.loads(row["redacted_categories"]) == ["token"] for row in recorded)
    assert token not in json.dumps(responses, ensure_ascii=False)
    assert token not in json.dumps(rows, ensure_ascii=False)


def test_read_rejects_owned_reservation_when_outbox_is_missing(lab_noworker, key):
    lab = lab_noworker
    data = _commit(lab, key)
    project, document_id = vault.parse_uri(data["uri"])
    path = vault.doc_path(lab.vault, project, document_id)
    marker = "clean bytes without outbox provenance"
    path.write_text(marker, encoding="utf-8")
    conn = lab.db()
    try:
        conn.execute("DELETE FROM outbox WHERE document_id=?", (document_id,))
    finally:
        conn.close()

    _, error, is_error = lab.call("memory_read", {"uris": [data["uri"]]})
    assert is_error and error["code"] == C.E_STATE_INVARIANT
    assert marker not in json.dumps(error, ensure_ascii=False)

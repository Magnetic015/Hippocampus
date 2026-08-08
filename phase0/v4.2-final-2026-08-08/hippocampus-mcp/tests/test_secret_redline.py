"""09 §10.3: the secret redline, end to end, with synthetic canaries only."""

from pathlib import Path

import pytest
from harness import CLIENTS, commit_args

from hippocampus import constants as C, failpoints, vault

CANARY = "AKIACANARY0EXAMPLE99"
DSN_CANARY = "postgresql://svc:canaryPass123@db.internal:5432/main"


@pytest.fixture(autouse=True)
def _clear():
    failpoints.clear()
    yield
    failpoints.clear()


def _no_canary_anywhere(lab, needle=CANARY):
    state_dir = Path(lab.db_path).parent
    for path in list(state_dir.rglob("*")) + list(Path(lab.vault).rglob("*")):
        if path.is_file():
            assert needle.encode() not in path.read_bytes(), f"canary leaked into {path.name}"
    for bank in lab.mock.banks.values():
        assert needle not in str(bank)
    assert all(needle not in str(item) for _, item in lab.mock.retain_log)


@pytest.mark.parametrize("field", ["title", "summary", "retrieval_text", "detail_body"])
def test_canary_in_memory_fields_rejected(lab, key, field):
    args = commit_args(key(), **{field: commit_args(key())[field] + " " + CANARY})
    status, data, is_error = lab.call("memory_commit", args)
    assert is_error and data["code"] == C.E_SECRET_REJECTED
    assert data["stored"] is False and data["indexed"] is False
    assert data["fields"] == [field]
    assert "AKIA" not in str(data["categories"])
    conn = lab.db()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM idempotency_reservation").fetchone()["c"] == 0
    finally:
        conn.close()
    _no_canary_anywhere(lab)


@pytest.mark.parametrize("field,value", [
    ("idempotency_key", CANARY),
    ("project", CANARY),
    ("type", CANARY),
    ("event_at", CANARY),
])
def test_canary_in_control_fields_rejected_by_schema_first(lab, key, field, value):
    args = commit_args(key(), **{field: value})
    _, data, is_error = lab.call("memory_commit", args)
    assert is_error and data["code"] in (C.E_SCHEMA_REJECTED, C.E_SECRET_REJECTED)
    _no_canary_anywhere(lab)


def test_canary_in_search_query_rejected_before_hindsight(lab):
    _, data, is_error = lab.call("memory_search", {"query": DSN_CANARY, "project": "commissioning"})
    assert is_error and data["code"] == C.E_SECRET_REJECTED
    assert lab.mock.retain_log == []
    row = lab.audit_rows()[0]
    assert row["query_hmac"] is None and row["query_len"] is None  # no HMAC for secret queries
    _no_canary_anywhere(lab, "canaryPass123")


def test_safe_query_records_hmac_and_length_only(lab):
    lab.call("memory_search", {"query": "写入链路 fsync", "project": "commissioning"})
    row = lab.audit_rows()[0]
    assert row["query_hmac"] and len(row["query_hmac"]) == 64
    assert row["query_len"] == len("写入链路 fsync")
    assert "写入链路" not in str(dict(row))


def test_encoded_and_split_canaries_rejected(lab, key):
    import base64
    b64 = base64.b64encode(b"password = SuperCanary123!").decode()
    for payload in (b64, "AKIA CANARY0 EXAMPLE99", "password%20%3D%20SuperCanary123%21"):
        _, data, is_error = lab.call("memory_commit",
                                     commit_args(key(), detail_body=f"备注 {payload}"))
        assert is_error and data["code"] == C.E_SECRET_REJECTED, payload


def test_valid_token_copied_into_body_is_rejected(lab, key):
    token = CLIENTS["mac-claude"]
    _, data, is_error = lab.call("memory_commit",
                                 commit_args(key(), detail_body=f"Authorization: Bearer {token}"))
    assert is_error and data["code"] == C.E_SECRET_REJECTED
    # ...while the same token in the header keeps working
    assert lab.rpc("tools/list")[0] == 200


def test_placeholders_and_references_pass(lab, key):
    body = ("使用 ${HIPPOCAMPUS_CLAUDE_TOKEN} 引用凭证,实际值存于外部管理器条目 "
            "op://vault/pi5-db,轮换日期 2026-08-01,占位符 <REDACTED>。")
    _, data, is_error = lab.call("memory_commit", commit_args(key(), detail_body=body))
    assert not is_error and data["stored"]


def test_canary_injected_after_commit_is_blocked_before_retain(lab_noworker, key):
    lab = lab_noworker
    _, data, _ = lab.call("memory_commit", commit_args(key()))
    project, doc = vault.parse_uri(data["uri"])
    path = vault.doc_path(lab.vault, project, doc)
    path.write_text(path.read_text("utf-8") + f"\n{CANARY}\n", encoding="utf-8")

    from hippocampus.worker import Worker
    Worker(lab.env).process_one()
    conn = lab.db()
    try:
        row = conn.execute("SELECT status, error_code FROM outbox").fetchone()
    finally:
        conn.close()
    assert row["status"] == "conflict" and row["error_code"] == C.E_HASH_MISMATCH
    assert lab.mock.retain_log == []


def test_policy_blocked_document_is_not_readable(lab_noworker, key):
    lab = lab_noworker
    _, data, _ = lab.call("memory_commit", commit_args(key()))
    conn = lab.db()
    try:
        conn.execute("UPDATE outbox SET status='policy_blocked', error_code='POLICY_BLOCKED'")
    finally:
        conn.close()
    _, read, is_error = lab.call("memory_read", {"uris": [data["uri"]]})
    assert is_error and read["code"] == C.E_POLICY_BLOCKED


def test_scanner_unavailable_fails_all_four_tools_closed(lab_noworker, key):
    lab = lab_noworker
    lab.env.scanner_down = True
    for tool, args in [("memory_commit", commit_args(key())),
                       ("memory_search", {"query": "x", "project": "commissioning"}),
                       ("memory_read", {"uris": ["memory://shared/commissioning/mem_20260808_ABCDEFGHJK"]}),
                       ("memory_status", {"document_id": "mem_20260808_ABCDEFGHJK"})]:
        status, data, is_error = lab.call(tool, args)
        assert is_error, tool
        assert data["code"] in (C.E_SCAN_UNAVAILABLE,), (tool, data)
    assert list(Path(lab.vault).rglob("*.md")) == []
    assert lab.mock.retain_log == []

    # health endpoints still report, readyz goes 503
    assert lab.raw(b"", method="GET", url=lab.url.replace("/mcp/", "/healthz"))[0] == 200
    assert lab.raw(b"", method="GET", url=lab.url.replace("/mcp/", "/readyz"))[0] == 503
    assert lab.raw(b"", method="GET", url=lab.url.replace("/mcp/", "/version"))[0] == 200


def test_worker_paused_while_scanner_down(lab_noworker, key):
    lab = lab_noworker
    lab.call("memory_commit", commit_args(key()))
    lab.env.scanner_down = True
    from hippocampus.worker import Worker
    assert Worker(lab.env).process_one() is False
    assert lab.mock.retain_log == []


def test_hindsight_error_body_with_canary_not_recorded(lab_noworker, key):
    lab = lab_noworker
    lab.call("memory_commit", commit_args(key()))
    lab.mock.fail_next(1)
    from hippocampus.worker import Worker
    Worker(lab.env).process_one()
    conn = lab.db()
    try:
        row = conn.execute("SELECT status, error_code FROM outbox").fetchone()
    finally:
        conn.close()
    assert row["status"] == "retry_wait"
    assert row["error_code"] in ("UPSTREAM_5XX", "HINDSIGHT_TIMEOUT")  # enum only, no body
    _no_canary_anywhere(lab)


def test_hindsight_never_receives_detail_or_full_hash(lab, key):
    marker = "只应存在于 Detail 的独有句子"
    _, data, _ = lab.call("memory_commit", commit_args(key(), detail_body=marker))
    assert lab.drain_worker()
    assert len(lab.mock.retain_log) == 1
    bank, item = lab.mock.retain_log[0]
    assert bank == C.BANK_COMMISSIONING
    payload = str(item)
    assert marker not in payload
    assert "## Detail" not in payload
    assert lab.vault not in payload and "/home/kkp" not in payload
    assert set(item["metadata"]) == {
        "uri", "project", "source_agent", "event_at", "index_title", "index_summary",
        "trust", "sensitivity", "retrieval_sha256"}
    assert all(isinstance(v, str) and v for v in item["metadata"].values())
    conn = lab.db()
    try:
        content_sha = conn.execute("SELECT desired_sha256 FROM outbox").fetchone()["desired_sha256"]
    finally:
        conn.close()
    assert content_sha not in payload  # full-MD hash stays in SQLite

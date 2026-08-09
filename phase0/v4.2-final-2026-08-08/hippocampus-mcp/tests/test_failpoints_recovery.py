"""09 §10.2 crash windows + 05 §5.3 startup recovery, via the test-build failpoints."""

from pathlib import Path

import pytest
from harness import commit_args

from hippocampus import constants as C, failpoints, vault
from hippocampus.recovery import run_startup_recovery


@pytest.fixture(autouse=True)
def _clear_failpoints():
    failpoints.clear()
    yield
    failpoints.clear()


def _reservations(lab):
    conn = lab.db()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM idempotency_reservation")]
    finally:
        conn.close()


def _outbox(lab):
    conn = lab.db()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM outbox")]
    finally:
        conn.close()


def test_failpoint_module_is_inert_without_test_build(monkeypatch):
    monkeypatch.delenv("HIPPOCAMPUS_TEST_BUILD", raising=False)
    failpoints.arm("x")
    failpoints.hit("x")  # no raise: production builds cannot trigger failpoints
    monkeypatch.setenv("HIPPOCAMPUS_TEST_BUILD", "1")


def test_crash_before_staging_leaves_retryable_and_no_file(lab_noworker, key):
    lab = lab_noworker
    k = key()
    failpoints.arm("commit.staging.before")
    _, data, is_error = lab.call("memory_commit", commit_args(k))
    assert is_error and data["retryable"]
    res = _reservations(lab)
    assert len(res) == 1 and res[0]["state"] == "retryable_failed"
    assert _outbox(lab) == []
    assert list(Path(lab.vault).rglob("*.md")) == []

    _, retry, is_error = lab.call("memory_commit", commit_args(k))
    assert not is_error and retry["stored"]
    assert _reservations(lab)[0]["document_id"] == res[0]["document_id"]  # same ID/URI reused


def test_crash_after_staging_before_prepared_is_recoverable(lab_noworker, key):
    lab = lab_noworker
    k = key()
    failpoints.arm("commit.staging.after")
    _, data, is_error = lab.call("memory_commit", commit_args(k))
    assert is_error
    assert _outbox(lab) == []
    assert list(Path(lab.vault).rglob(".hippocampus-*.staging")) == []  # staging cleaned up

    _, retry, is_error = lab.call("memory_commit", commit_args(k))
    assert not is_error and retry["stored"] and retry["index_state"] == "index_pending"


def test_crash_between_prepared_and_rename_recovers_via_startup(lab_noworker, key):
    lab = lab_noworker
    k = key()
    failpoints.arm("commit.rename.before")
    _, data, is_error = lab.call("memory_commit", commit_args(k))
    assert not is_error
    assert data["accepted"] and not data["stored"]
    assert data["index_state"] == "recovery_pending"
    assert _outbox(lab)[0]["status"] == "prepared"
    staging = list(Path(lab.vault).rglob(".hippocampus-*.staging"))
    assert len(staging) == 1

    stats = run_startup_recovery(lab.env)
    assert stats["promoted"] == 1
    assert _outbox(lab)[0]["status"] == "ready"
    assert _reservations(lab)[0]["state"] == "stored"
    assert list(Path(lab.vault).rglob(".hippocampus-*.staging")) == []
    project, doc = vault.parse_uri(data["uri"])
    assert vault.doc_path(lab.vault, project, doc).exists()


def test_crash_after_rename_before_ready_recovers(lab_noworker, key):
    lab = lab_noworker
    failpoints.arm("commit.ready.txn")
    _, data, is_error = lab.call("memory_commit", commit_args(key()))
    assert not is_error and data["stored"] and data["index_state"] == "recovery_pending"
    assert _outbox(lab)[0]["status"] == "prepared"

    stats = run_startup_recovery(lab.env)
    assert stats["readied"] == 1
    assert _outbox(lab)[0]["status"] == "ready"


def test_durability_gap_marks_both_tables_and_readyz(lab_noworker, key):
    lab = lab_noworker
    failpoints.arm("commit.rename.before")
    _, data, _ = lab.call("memory_commit", commit_args(key()))
    for staging in Path(lab.vault).rglob(".hippocampus-*.staging"):
        staging.unlink()

    stats = run_startup_recovery(lab.env)
    assert stats["durability_gap"] == 1
    assert _outbox(lab)[0]["status"] == "conflict"
    assert _outbox(lab)[0]["error_code"] == C.E_DURABILITY_GAP
    assert _reservations(lab)[0]["state"] == "conflict"
    assert lab.env.health["durability_gap"] is True
    status, _ = lab.raw(b"", method="GET", url=lab.url.replace("/mcp/", "/readyz"))
    assert status == 503


def test_recovery_rescans_under_upgraded_policy(lab_noworker, key):
    """Same bytes, stricter policy after restart: promotion must be blocked."""
    lab = lab_noworker
    failpoints.arm("commit.rename.before")
    lab.call("memory_commit", commit_args(key()))
    assert _outbox(lab)[0]["status"] == "prepared"

    lab.env.scan_text = lambda text: {"credential"} if "Hippocampus" in text else set()
    stats = run_startup_recovery(lab.env)
    assert stats["policy_blocked"] == 1 and stats["promoted"] == 0
    assert _outbox(lab)[0]["status"] == "policy_blocked"
    assert list(Path(lab.vault).rglob("*.md")) == []  # never renamed into the Vault


def test_tampered_staging_is_conflict_not_gap(lab_noworker, key):
    lab = lab_noworker
    failpoints.arm("commit.rename.before")
    lab.call("memory_commit", commit_args(key()))
    staging = list(Path(lab.vault).rglob(".hippocampus-*.staging"))[0]
    staging.write_text(staging.read_text("utf-8") + "\n外部改动\n", encoding="utf-8")

    stats = run_startup_recovery(lab.env)
    assert stats["hash_mismatch"] == 1 and stats["durability_gap"] == 0
    row = _outbox(lab)[0]
    assert row["status"] == "conflict" and row["error_code"] == C.E_HASH_MISMATCH


def test_recovery_paused_when_scanner_down(lab_noworker, key):
    lab = lab_noworker
    failpoints.arm("commit.rename.before")
    lab.call("memory_commit", commit_args(key()))
    lab.env.scanner_down = True
    stats = run_startup_recovery(lab.env)
    assert stats["promoted"] == 0 and stats["policy_blocked"] == 0
    assert _outbox(lab)[0]["status"] == "prepared"


def test_unowned_orphans_counted_never_adopted(lab_noworker, key):
    lab = lab_noworker
    lab.call("memory_commit", commit_args(key()))
    d = Path(lab.vault) / "shared" / "commissioning"
    (d / "mem_20260808_ABCDEFGHJK.md").write_text("---\nid: x\n---\n", encoding="utf-8")
    (d / ".hippocampus-deadbeef.staging").write_text("orphan", encoding="utf-8")

    stats = run_startup_recovery(lab.env)
    assert stats["unowned_orphan"] == 2
    assert len(_outbox(lab)) == 1  # no events invented for orphans
    assert (d / "mem_20260808_ABCDEFGHJK.md").exists()  # never deleted


def test_stale_audit_rows_converge(lab_noworker, key):
    lab = lab_noworker
    lab.rpc("tools/list")
    lab.call("memory_commit", commit_args(key()))
    conn = lab.db()
    try:
        conn.execute("UPDATE audit SET state='started', process_instance_id='other-instance'")
    finally:
        conn.close()

    run_startup_recovery(lab.env)
    rows = lab.audit_rows()
    assert all(r["state"] != "started" for r in rows)
    commit_rows = [r for r in rows if r["event_id"]]
    assert all(r["state"] == "ok" for r in commit_rows)
    other = [r for r in rows if not r["event_id"] and r["client_id"]]
    assert all(r["state"] == "interrupted" and r["outcome_code"] == C.OUTCOME_FINALIZE_LOST
               for r in other)


def test_recovery_pending_audit_converges_after_artifact_promotion(lab_noworker, key):
    lab = lab_noworker
    failpoints.arm("commit.rename.before")
    lab.call("memory_commit", commit_args(key()))
    conn = lab.db()
    try:
        row = conn.execute("SELECT * FROM audit").fetchone()
        assert row["state"] == "recovery_pending"
        conn.execute(
            "UPDATE audit SET process_instance_id='prior-process',"
            " normalized_ref='safe-history', latency_ms=17,"
            " redacted_fields='[\"title\"]' WHERE request_id=?",
            (row["request_id"],),
        )
        before = dict(conn.execute("SELECT * FROM audit").fetchone())
    finally:
        conn.close()

    stats = run_startup_recovery(lab.env)
    after = lab.audit_rows()[0]

    assert stats["promoted"] == 1 and stats["audit_converged"] == 1
    assert after["state"] == "ok" and after["outcome_code"] == C.OUTCOME_COMMIT_OK
    for field in (
            "request_id", "ts", "process_instance_id", "root_request_id", "event_id",
            "client_id", "operation_enum", "tool_enum", "normalized_ref", "latency_ms",
            "scan_policy_version", "redacted_fields", "redacted_categories"):
        assert after[field] == before[field]


def test_recovery_pending_audit_becomes_error_after_artifact_conflict(
        lab_noworker, key):
    lab = lab_noworker
    failpoints.arm("commit.rename.before")
    lab.call("memory_commit", commit_args(key()))
    staging = list(Path(lab.vault).rglob(".hippocampus-*.staging"))[0]
    staging.write_bytes(b"tampered prior-process artifact")
    conn = lab.db()
    try:
        conn.execute("UPDATE audit SET process_instance_id='prior-process'")
    finally:
        conn.close()

    stats = run_startup_recovery(lab.env)
    row = lab.audit_rows()[0]

    assert stats["hash_mismatch"] == 1 and stats["audit_converged"] == 1
    assert row["state"] == "error" and row["outcome_code"] == C.OUTCOME_FINALIZE_LOST


def test_expired_reservation_lease_released_not_rebuilt(lab_noworker, key):
    lab = lab_noworker
    k = key()
    failpoints.arm("commit.staging.before")
    lab.call("memory_commit", commit_args(k))
    conn = lab.db()
    try:
        conn.execute("UPDATE idempotency_reservation SET lease_owner='dead', lease_expires_at=1")
    finally:
        conn.close()

    stats = run_startup_recovery(lab.env)
    assert stats["released_leases"] == 1
    assert list(Path(lab.vault).rglob("*.md")) == []  # never reconstructed without payload
    _, retry, is_error = lab.call("memory_commit", commit_args(k))
    assert not is_error and retry["stored"]

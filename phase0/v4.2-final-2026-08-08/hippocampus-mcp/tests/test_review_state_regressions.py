"""Regression coverage for verified PR review findings in state and Vault recovery."""

import os
import stat
from pathlib import Path

import pytest
from harness import commit_args

from hippocampus import constants as C, failpoints, statemachine as sm, vault
from hippocampus.recovery import run_startup_recovery
from hippocampus.worker import Worker


@pytest.fixture(autouse=True)
def _clear_failpoints():
    failpoints.clear()
    yield
    failpoints.clear()


def _prepared_paths(lab, key: str):
    failpoints.arm("commit.rename.before")
    _, data, is_error = lab.call("memory_commit", commit_args(key))
    assert not is_error and data["index_state"] == "recovery_pending"
    conn = lab.db()
    try:
        res = conn.execute("SELECT * FROM idempotency_reservation").fetchone()
        out = conn.execute("SELECT * FROM outbox").fetchone()
        project, document_id = vault.parse_uri(res["uri"])
        staging = vault.staging_path(lab.vault, project, res["event_id"])
        final = vault.doc_path(lab.vault, project, document_id)
        return staging, final, out["desired_sha256"]
    finally:
        conn.close()


def _outbox(lab):
    conn = lab.db()
    try:
        return dict(conn.execute("SELECT * FROM outbox").fetchone())
    finally:
        conn.close()


def _reservation_state(lab):
    conn = lab.db()
    try:
        return conn.execute("SELECT state FROM idempotency_reservation").fetchone()["state"]
    finally:
        conn.close()


def test_expired_indexing_lease_is_atomically_reclaimed(lab_noworker, key):
    lab = lab_noworker
    lab.call("memory_commit", commit_args(key()))
    conn = lab.db()
    try:
        first = sm.claim_next_event(conn, "owner-a")
        assert first is not None and first["status"] == "indexing" and first["attempt"] == 1
        conn.execute("UPDATE outbox SET lease_expires_at=1 WHERE event_id=?",
                     (first["event_id"],))
        reclaimed = sm.claim_next_event(conn, "owner-b")
    finally:
        conn.close()

    assert reclaimed is not None
    assert reclaimed["status"] == "indexing"
    assert reclaimed["lease_owner"] == "owner-b"
    assert reclaimed["attempt"] == 2


def test_worker_hash_mismatch_marks_both_rows_conflicted(lab_noworker, key):
    lab = lab_noworker
    idempotency_key = key()
    args = commit_args(idempotency_key)
    _, committed, is_error = lab.call("memory_commit", args)
    assert not is_error
    project, document_id = vault.parse_uri(committed["uri"])
    final = vault.doc_path(lab.vault, project, document_id)
    final.write_bytes(b"tampered after commit")

    assert Worker(lab.env).process_one()
    conn = lab.db()
    try:
        row = conn.execute(
            "SELECT r.state AS reservation_state, o.status AS outbox_status, o.error_code"
            " FROM idempotency_reservation r JOIN outbox o USING(event_id)"
        ).fetchone()
    finally:
        conn.close()

    assert dict(row) == {
        "reservation_state": "conflict",
        "outbox_status": "conflict",
        "error_code": C.E_HASH_MISMATCH,
    }
    _, replay, replay_error = lab.call("memory_commit", args)
    assert not replay_error
    assert replay["stored"] is False and replay["index_state"] == "conflict"


@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory", "mode"])
def test_worker_unsafe_artifact_converges_both_rows(lab_noworker, key, unsafe_kind):
    lab = lab_noworker
    _, committed, is_error = lab.call("memory_commit", commit_args(key()))
    assert not is_error
    project, document_id = vault.parse_uri(committed["uri"])
    final = vault.doc_path(lab.vault, project, document_id)

    if unsafe_kind == "symlink":
        target = Path(lab.tmp) / "worker-symlink-target"
        target.write_bytes(final.read_bytes())
        target.chmod(0o600)
        final.unlink()
        final.symlink_to(target)
    elif unsafe_kind == "directory":
        final.unlink()
        final.mkdir()
    else:
        final.chmod(0o644)

    assert Worker(lab.env).process_one()
    conn = lab.db()
    try:
        row = conn.execute(
            "SELECT r.state AS reservation_state, r.lease_owner AS reservation_lease,"
            " o.status AS outbox_status, o.error_code, o.lease_owner AS outbox_lease"
            " FROM idempotency_reservation r JOIN outbox o USING(event_id)"
        ).fetchone()
    finally:
        conn.close()

    assert dict(row) == {
        "reservation_state": "conflict",
        "reservation_lease": None,
        "outbox_status": "conflict",
        "error_code": C.E_HASH_MISMATCH,
        "outbox_lease": None,
    }
    assert lab.mock.retain_log == []


def test_write_staging_retries_short_writes(tmp_path, monkeypatch):
    staging = tmp_path / "short-write.staging"
    payload = b"0123456789abcdef"
    real_write = os.write
    calls = 0

    def short_write(fd, data):
        nonlocal calls
        calls += 1
        return real_write(fd, data[:3])

    monkeypatch.setattr(vault.os, "write", short_write)
    vault.write_staging(staging, payload)

    assert staging.read_bytes() == payload
    assert calls > 1


def test_write_staging_rejects_zero_byte_write(tmp_path, monkeypatch):
    staging = tmp_path / "zero-write.staging"
    monkeypatch.setattr(vault.os, "write", lambda _fd, _data: 0)

    with pytest.raises(OSError, match="no progress"):
        vault.write_staging(staging, b"must not be reported durable")


def test_recovery_rejects_tampered_final_even_with_valid_staging(lab_noworker, key):
    lab = lab_noworker
    staging, final, _ = _prepared_paths(lab, key())
    altered = b"externally altered final"
    final.write_bytes(altered)
    final.chmod(0o600)

    stats = run_startup_recovery(lab.env)
    out = _outbox(lab)

    assert stats["hash_mismatch"] == 1 and stats["promoted"] == 0
    assert out["status"] == "conflict" and out["error_code"] == C.E_HASH_MISMATCH
    assert _reservation_state(lab) == "conflict"
    assert final.read_bytes() == altered
    assert staging.exists()


def test_recovery_converges_symlinked_final_path(lab_noworker, key):
    lab = lab_noworker
    staging, final, _ = _prepared_paths(lab, key())
    target = Path(lab.tmp) / "recovery-symlink-target"
    target.write_bytes(staging.read_bytes())
    target.chmod(0o600)
    final.symlink_to(target)

    stats = run_startup_recovery(lab.env)

    assert stats["hash_mismatch"] == 1 and stats["promoted"] == 0
    assert _outbox(lab)["status"] == "conflict"
    assert _reservation_state(lab) == "conflict"
    assert final.is_symlink()


def test_recovery_rejects_tampered_staging_even_with_valid_final(lab_noworker, key):
    lab = lab_noworker
    staging, final, _ = _prepared_paths(lab, key())
    expected = staging.read_bytes()
    final.write_bytes(expected)
    final.chmod(0o600)
    staging.write_bytes(b"externally altered staging")

    stats = run_startup_recovery(lab.env)
    out = _outbox(lab)

    assert stats["hash_mismatch"] == 1 and stats["readied"] == 0
    assert out["status"] == "conflict" and out["error_code"] == C.E_HASH_MISMATCH
    assert _reservation_state(lab) == "conflict"
    assert final.read_bytes() == expected
    assert staging.read_bytes() == b"externally altered staging"


@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory", "mode"])
def test_recovery_rejects_unsafe_staging_artifact(lab_noworker, key, unsafe_kind):
    lab = lab_noworker
    staging, final, _ = _prepared_paths(lab, key())

    if unsafe_kind == "symlink":
        target = Path(lab.tmp) / "matching-target"
        target.write_bytes(staging.read_bytes())
        target.chmod(0o600)
        staging.unlink()
        staging.symlink_to(target)
    elif unsafe_kind == "directory":
        staging.unlink()
        staging.mkdir()
    else:
        staging.chmod(0o644)

    stats = run_startup_recovery(lab.env)
    out = _outbox(lab)

    assert stats["hash_mismatch"] == 1 and stats["promoted"] == 0
    assert out["status"] == "conflict" and out["error_code"] == C.E_HASH_MISMATCH
    assert _reservation_state(lab) == "conflict"
    assert not final.exists()
    if unsafe_kind == "symlink":
        assert staging.is_symlink()
    elif unsafe_kind == "directory":
        assert staging.is_dir()
    else:
        assert stat.S_IMODE(staging.stat().st_mode) == 0o644

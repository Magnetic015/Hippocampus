"""P0.10 / 09 §10.2: reservation, idempotency, concurrency, fsync chain."""

import concurrent.futures as cf
import json
import sqlite3
from pathlib import Path

from harness import commit_args

from hippocampus import canonical, constants as C, vault
from hippocampus.mdrender import parse_frontmatter


def test_commit_happy_path(lab, key):
    status, data, is_error = lab.call("memory_commit", commit_args(key()))
    assert status == 200 and not is_error
    assert data["accepted"] and data["stored"] and not data["indexed"]
    assert data["index_state"] == "index_pending"
    project, doc = vault.parse_uri(data["uri"])
    path = vault.doc_path(lab.vault, project, doc)
    assert path.exists() and path.stat().st_mode & 0o777 == 0o600
    meta = parse_frontmatter(path.read_bytes())
    assert meta["uri"] == data["uri"] and meta["trust"] == "agent"
    assert meta["scope"] == "shared" and meta["sensitivity"] == "internal"
    assert meta["event_at"] == "unset"


def test_no_staging_file_survives(lab, key):
    lab.call("memory_commit", commit_args(key()))
    leftovers = list(Path(lab.vault).rglob(".hippocampus-*.staging"))
    assert leftovers == []


def test_same_key_same_payload_replays_same_event(lab, key):
    k = key()
    _, first, _ = lab.call("memory_commit", commit_args(k))
    _, second, _ = lab.call("memory_commit", commit_args(k))
    assert first["document_id"] == second["document_id"]
    assert first["uri"] == second["uri"]
    conn = lab.db()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM idempotency_reservation").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 1
        rows = conn.execute("SELECT request_id, root_request_id, event_id, state FROM audit"
                            " WHERE event_id IS NOT NULL").fetchall()
    finally:
        conn.close()
    assert len(rows) == 2  # 1 event : N audit requests
    assert len({r["request_id"] for r in rows}) == 2
    assert len({r["event_id"] for r in rows}) == 1
    assert len({r["root_request_id"] for r in rows}) == 1
    assert {r["state"] for r in rows} == {"ok", "idempotent_replay"}


def test_semantically_equal_event_times_replay_and_migrate_legacy_hash(lab, key):
    utc_key = key()
    utc_z = commit_args(utc_key, event_at="2026-08-01T01:30:00.000Z")
    _, first, is_error = lab.call("memory_commit", utc_z)
    assert not is_error

    legacy_utc_hmac = canonical.payload_hmac(
        lab.env.idem_key,
        {
            "title": utc_z["title"],
            "summary": utc_z["summary"],
            "retrieval_text": utc_z["retrieval_text"],
            "detail_body": utc_z["detail_body"],
            "event_at": utc_z["event_at"],
            "project": utc_z["project"],
            "type": utc_z["type"],
        },
    )
    conn = lab.db()
    try:
        conn.execute(
            "UPDATE idempotency_reservation SET payload_hmac=? WHERE idempotency_key=?",
            (legacy_utc_hmac, utc_key),
        )
    finally:
        conn.close()

    _, replay, is_error = lab.call(
        "memory_commit", commit_args(utc_key, event_at="2026-08-01T01:30:00+00:00"))
    assert not is_error and replay["document_id"] == first["document_id"]

    unset_key = key()
    unset_args = commit_args(unset_key)
    _, unset_first, is_error = lab.call("memory_commit", unset_args)
    assert not is_error
    legacy_absent_hmac = canonical.payload_hmac(
        lab.env.idem_key,
        {
            "title": unset_args["title"],
            "summary": unset_args["summary"],
            "retrieval_text": unset_args["retrieval_text"],
            "detail_body": unset_args["detail_body"],
            "event_at": None,
            "project": unset_args["project"],
            "type": unset_args["type"],
        },
    )
    conn = lab.db()
    try:
        conn.execute(
            "UPDATE idempotency_reservation SET payload_hmac=? WHERE idempotency_key=?",
            (legacy_absent_hmac, unset_key),
        )
    finally:
        conn.close()

    _, unset_replay, is_error = lab.call(
        "memory_commit", commit_args(unset_key, event_at="unset"))
    assert not is_error and unset_replay["document_id"] == unset_first["document_id"]
    conn = lab.db()
    try:
        migrated_hmac = conn.execute(
            "SELECT payload_hmac FROM idempotency_reservation WHERE idempotency_key=?",
            (unset_key,),
        ).fetchone()["payload_hmac"]
    finally:
        conn.close()
    assert migrated_hmac != legacy_absent_hmac


def test_same_key_different_payload_is_conflict(lab, key):
    k = key()
    lab.call("memory_commit", commit_args(k))
    _, data, is_error = lab.call("memory_commit", commit_args(k, title="不同标题"))
    assert is_error and data["code"] == C.E_IDEMPOTENCY_CONFLICT
    conn = lab.db()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 1
    finally:
        conn.close()


def test_legacy_event_migration_never_accepts_other_render_collisions(lab, key):
    k = key()
    original = commit_args(k)
    original.pop("detail_body")
    _, _, is_error = lab.call("memory_commit", original)
    assert not is_error

    _, error, is_error = lab.call("memory_commit", commit_args(k, detail_body=""))
    assert is_error and error["code"] == C.E_IDEMPOTENCY_CONFLICT


def test_different_keys_are_independent(lab, key):
    _, a, _ = lab.call("memory_commit", commit_args(key()))
    _, b, _ = lab.call("memory_commit", commit_args(key()))
    assert a["document_id"] != b["document_id"]


def test_concurrent_same_key_same_payload_single_event(lab, key):
    k = key()
    args = commit_args(k)
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        results = [f.result() for f in
                   [pool.submit(lab.call, "memory_commit", args) for _ in range(6)]]
    docs = {r[1]["document_id"] for r in results if not r[2]}
    assert len(docs) == 1
    conn = lab.db()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM idempotency_reservation").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 1
    finally:
        conn.close()
    project, doc = vault.parse_uri(results[0][1]["uri"])
    assert len(list((Path(lab.vault) / "shared" / project).glob("*.md"))) == 1


def test_concurrent_same_key_different_payload_one_winner(lab, key):
    k = key()
    variants = [commit_args(k, title=f"标题-{i}") for i in range(4)]
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result() for f in
                   [pool.submit(lab.call, "memory_commit", v) for v in variants]]
    ok = [r for r in results if not r[2]]
    conflicts = [r for r in results if r[2] and r[1]["code"] == C.E_IDEMPOTENCY_CONFLICT]
    assert len(ok) == 1 and len(conflicts) == 3


def test_state_check_constraints_enforced(lab, key):
    lab.call("memory_commit", commit_args(key()))
    conn = lab.db()
    try:
        for table, col, bad in [("outbox", "status", "superseded"),
                                ("outbox", "status", "recovery_pending"),
                                ("idempotency_reservation", "state", "made_up"),
                                ("audit", "state", "made_up")]:
            try:
                conn.execute(f"UPDATE {table} SET {col}=?", (bad,))
                raise AssertionError(f"{table}.{col} accepted {bad}")
            except sqlite3.IntegrityError:
                pass
    finally:
        conn.close()


def test_index_state_projection_matrix():
    p = C.index_state_projection
    assert p("ready", "stored") == "index_pending"
    assert p("retry_wait", "stored") == "index_pending"
    assert p("prepared", "prepared") == "recovery_pending"
    assert p("indexed", "stored") == "indexed"
    assert p("dead", "stored") == "dead"
    assert p(None, "reserved") == "recovery_pending"
    assert p(None, "retryable_failed") == "recovery_pending"
    assert p(None, "rejected") == "rejected"
    assert p(None, "stored") == "conflict"  # STATE_INVARIANT_BROKEN


def test_status_tool_reports_safe_fields(lab, key):
    k = key()
    _, data, _ = lab.call("memory_commit", commit_args(k))
    lab.drain_worker()
    _, by_doc, err = lab.call("memory_status", {"document_id": data["document_id"]})
    assert not err and by_doc["index_state"] == "indexed"
    _, by_key, err = lab.call("memory_status", {"idempotency_key": k})
    assert not err and by_key["document_id"] == data["document_id"]
    assert set(by_key) <= {"document_id", "index_state", "attempt", "next_attempt_at", "error_code"}


def test_status_requires_single_key(lab, key):
    _, data, is_error = lab.call("memory_status", {})
    assert is_error and data["fields"] == ["status_key"]


def test_limits_rejected_before_any_write(lab, key):
    conn = lab.db()
    try:
        cases = [
            ("summary", commit_args(key(), summary="太短")),
            ("retrieval_text", commit_args(key(), retrieval_text="太短的检索文本")),
            ("type", commit_args(key(), type="not_a_type")),
            ("project", commit_args(key(), project="BadProject")),
            ("idempotency_key", commit_args("not-a-uuid4")),
        ]
        for name, args in cases:
            _, data, is_error = lab.call("memory_commit", args)
            assert is_error, name
        assert conn.execute("SELECT COUNT(*) c FROM idempotency_reservation").fetchone()["c"] == 0
        assert list(Path(lab.vault).rglob("*.md")) == []
    finally:
        conn.close()


def test_event_at_roundtrip(lab, key):
    _, data, _ = lab.call("memory_commit",
                          commit_args(key(), event_at="2026-08-01T09:30:00+08:00"))
    project, doc = vault.parse_uri(data["uri"])
    meta = parse_frontmatter(vault.doc_path(lab.vault, project, doc).read_bytes())
    assert meta["event_at"] == "2026-08-01T09:30:00+08:00"
    _, err, is_error = lab.call("memory_commit", commit_args(key(), event_at="2026-08-01 09:30"))
    assert is_error and err["fields"] == ["event_at"]


def test_read_returns_authorized_body_only(lab, key):
    _, data, _ = lab.call("memory_commit", commit_args(key()))
    _, read, is_error = lab.call("memory_read", {"uris": [data["uri"]]})
    assert not is_error and "## Detail" in read["documents"][0]["markdown"]
    _, denied, is_error = lab.call(
        "memory_read", {"uris": ["memory://shared/global/mem_20260808_0000000000"]})
    assert is_error and denied["code"] == C.E_AUTHZ_DENIED


def test_read_rejects_traversal_and_orphans(lab, key):
    for bad in ["memory://shared/../../etc/passwd", "memory://shared/commissioning/../x",
                "file:///etc/passwd", "memory://shared/commissioning/notadoc"]:
        _, data, is_error = lab.call("memory_read", {"uris": [bad]})
        assert is_error and data["code"] == C.E_SCHEMA_REJECTED, bad

    orphan_dir = Path(lab.vault) / "shared" / "commissioning"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / "mem_20260808_0RPHAN00000.md"
    orphan.write_text("---\nid: x\n---\n\n## Detail\n\n安全内容\n", encoding="utf-8")
    _, data, is_error = lab.call(
        "memory_read", {"uris": ["memory://shared/commissioning/mem_20260808_0RPHAN00000"]})
    assert is_error and data["code"] == C.E_NOT_FOUND  # no provenance -> never readable

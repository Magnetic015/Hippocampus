"""09 §10.1/§10.4/§10.5: retain payload, R12 merge, cross-client sharing, degradation."""

import json
import time
from pathlib import Path

from harness import commit_args

from hippocampus import constants as C, vault


def _commit_and_index(lab, key_factory, **over):
    _, data, is_error = lab.call("memory_commit", commit_args(key_factory(), **over))
    assert not is_error, data
    assert lab.drain_worker()
    return data


def test_retain_timeout_exceeds_llm_extraction_budget():
    """Hindsight retain runs LLM extraction (~40s observed on Pi5). A short
    client timeout makes the worker abandon in-flight retains and re-run the
    extraction on every attempt, so retain and recall need separate budgets."""
    from hippocampus.hindsight_client import (
        DEFAULT_RECALL_TIMEOUT_S, DEFAULT_RETAIN_TIMEOUT_S, HindsightClient)
    assert DEFAULT_RETAIN_TIMEOUT_S >= 120
    assert DEFAULT_RECALL_TIMEOUT_S <= 30  # search stays interactive
    c = HindsightClient("http://x", "t")
    assert c.retain_timeout > c.recall_timeout


def test_retain_payload_shape(lab, key):
    _commit_and_index(lab, key, event_at="2026-08-01T09:30:00+08:00")
    bank, item = lab.mock.retain_log[0]
    assert bank == C.BANK_COMMISSIONING
    assert item["update_mode"] == "replace"
    assert item["timestamp"] == "2026-08-01T09:30:00+08:00"
    assert item["metadata"]["event_at"] == item["timestamp"]
    assert item["tags"][0].startswith("src:")
    assert "scope:shared" in item["tags"] and "trust:agent" in item["tags"]


def test_unset_event_time_is_explicit(lab, key):
    _commit_and_index(lab, key)
    _, item = lab.mock.retain_log[0]
    assert item["timestamp"] == "unset"  # never omitted, never null (R1)
    assert item["metadata"]["event_at"] == "unset"
    assert "timestamp" in item


def test_async_false_always_sent(lab, key):
    _commit_and_index(lab, key)
    assert lab.mock.retain_log  # mock asserts async is False on every retain


def test_replace_upsert_no_duplicates(lab_noworker, key):
    lab = lab_noworker
    _, data, _ = lab.call("memory_commit", commit_args(key()))
    from hippocampus.worker import Worker
    w = Worker(lab.env)
    assert w.process_one()
    conn = lab.db()
    try:  # simulate at-least-once redelivery after a lost ack
        conn.execute("UPDATE outbox SET status='ready', lease_owner=NULL, lease_expires_at=NULL")
    finally:
        conn.close()
    assert w.process_one()
    assert len(lab.mock.retain_log) == 2  # delivered twice...
    assert len(lab.mock.banks[C.BANK_COMMISSIONING]) == 1  # ...one current document


def test_search_merges_multiple_facts_into_one_card(lab, key):
    data = _commit_and_index(lab, key)
    _, result, is_error = lab.call("memory_search",
                                   {"query": "Hippocampus 写入链路 reservation", "project": "commissioning"})
    assert not is_error
    docs = [c["document_id"] for c in result["results"]]
    assert docs.count(data["document_id"]) == 1  # mock returns 2 facts per document
    card = result["results"][0]
    assert set(card) == {"document_id", "uri", "index_title", "index_summary", "safe_snippet",
                         "tags", "source_agent", "trust", "event_at", "index_state"}
    assert card["index_state"] == "indexed"
    assert "## Detail" not in json.dumps(card, ensure_ascii=False)
    # `text`/`scores.final` are the real v0.9.0 field names; reading the wrong
    # ones silently produced empty snippets and flat ranking in Phase 1.
    assert card["safe_snippet"], "snippet must be populated from the fact body"
    assert card["event_at"] != "1999-01-01T00:00:00+00:00"  # mentioned_at must not leak


def test_search_caps_at_five_unique_documents(lab, key):
    for _ in range(8):
        _commit_and_index(lab, key)
    _, result, _ = lab.call("memory_search",
                            {"query": "Hippocampus 写入链路", "project": "commissioning"})
    ids = [c["document_id"] for c in result["results"]]
    assert len(ids) == C.SEARCH_MAX_CARDS and len(set(ids)) == C.SEARCH_MAX_CARDS


def test_conflicting_metadata_group_dropped(lab, key):
    data = _commit_and_index(lab, key)
    stored = lab.mock.banks[C.BANK_COMMISSIONING][data["document_id"]]
    stored["metadata"] = dict(stored["metadata"], uri="memory://shared/other/mem_20260808_ABCDEFGHJK")
    _, result, _ = lab.call("memory_search",
                            {"query": "Hippocampus 写入链路", "project": "commissioning"})
    assert result["results"] == []  # URI/project mismatch drops the whole group


def test_project_isolation_is_strict(lab, key):
    from harness import Lab
    data = _commit_and_index(lab, key)
    stored = lab.mock.banks[C.BANK_COMMISSIONING][data["document_id"]]
    stored["tags"] = [t for t in stored["tags"] if not t.startswith("project:")]
    _, result, _ = lab.call("memory_search",
                            {"query": "Hippocampus 写入链路", "project": "commissioning"})
    assert result["results"] == []  # untagged entries never leak in


def test_cross_client_sharing_keeps_original_src(lab, key):
    data = _commit_and_index(lab, key)
    for reader in ("mac-codex", "dockerNode-openclaw"):
        _, result, is_error = lab.call(
            "memory_search", {"query": "Hippocampus 写入链路", "project": "commissioning"},
            client=reader)
        assert not is_error
        hit = [c for c in result["results"] if c["document_id"] == data["document_id"]]
        assert hit and hit[0]["source_agent"] == "mac-claude"  # no implicit src filter
        _, read, is_error = lab.call("memory_read", {"uris": [data["uri"]]}, client=reader)
        assert not is_error and read["documents"][0]["uri"] == data["uri"]


def test_search_source_filter_only_narrows(lab, key):
    _commit_and_index(lab, key)
    _, mine, _ = lab.call("memory_search", {"query": "Hippocampus 写入链路",
                                            "project": "commissioning", "source": "mac-claude"})
    _, theirs, _ = lab.call("memory_search", {"query": "Hippocampus 写入链路",
                                              "project": "commissioning", "source": "mac-codex"})
    assert len(mine["results"]) == 1 and theirs["results"] == []


def test_commit_and_read_survive_hindsight_outage(lab_noworker, key):
    lab = lab_noworker
    lab.mock.stop()  # backend gone
    _, data, is_error = lab.call("memory_commit", commit_args(key()))
    assert not is_error and data["stored"] and data["index_state"] == "index_pending"
    _, read, is_error = lab.call("memory_read", {"uris": [data["uri"]]})
    assert not is_error
    _, st, is_error = lab.call("memory_status", {"document_id": data["document_id"]})
    assert not is_error and st["index_state"] == "index_pending"
    _, search, is_error = lab.call("memory_search", {"query": "写入", "project": "commissioning"})
    assert is_error and search["code"] == C.E_INDEX_UNAVAILABLE  # explicit, no silent fallback


def test_backlog_drains_after_recovery(lab_noworker, key):
    lab = lab_noworker
    from hippocampus.worker import Worker
    w = Worker(lab.env)
    lab.call("memory_commit", commit_args(key()))
    lab.mock.fail_next(1)
    w.process_one()
    conn = lab.db()
    try:
        row = conn.execute("SELECT status, attempt, next_attempt_at FROM outbox").fetchone()
        assert row["status"] == "retry_wait" and row["attempt"] == 1
        conn.execute("UPDATE outbox SET next_attempt_at=0")  # fast-forward backoff
    finally:
        conn.close()
    assert w.process_one()
    conn = lab.db()
    try:
        row = conn.execute("SELECT status, attempt FROM outbox").fetchone()
    finally:
        conn.close()
    assert row["status"] == "indexed" and row["attempt"] == 2


def test_dead_after_max_attempts(lab_noworker, key):
    lab = lab_noworker
    from hippocampus.worker import Worker
    w = Worker(lab.env)
    lab.call("memory_commit", commit_args(key()))
    for _ in range(C.MAX_ATTEMPTS + 1):
        lab.mock.fail_next(1)
        w.process_one()
        conn = lab.db()
        try:
            conn.execute("UPDATE outbox SET next_attempt_at=0")
            row = conn.execute("SELECT status FROM outbox").fetchone()
        finally:
            conn.close()
        if row["status"] == "dead":
            break
    assert row["status"] == "dead"
    _, st, _ = lab.call("memory_status", {"document_id":
                                          lab.audit_rows()[0]["normalized_ref"] or
                                          _first_doc(lab)})
    assert st["index_state"] == "dead" and st["error_code"]


def _first_doc(lab):
    conn = lab.db()
    try:
        return conn.execute("SELECT document_id FROM outbox").fetchone()["document_id"]
    finally:
        conn.close()


def test_worker_lease_prevents_double_flight(lab_noworker, key):
    lab = lab_noworker
    lab.call("memory_commit", commit_args(key()))
    from hippocampus import statemachine as sm
    conn = lab.db()
    try:
        first = sm.claim_next_event(conn, "owner-a")
        second = sm.claim_next_event(conn, "owner-b")
    finally:
        conn.close()
    assert first is not None and second is None  # single flight


def test_expired_worker_lease_can_be_taken_over(lab_noworker, key):
    lab = lab_noworker
    lab.call("memory_commit", commit_args(key()))
    from hippocampus import statemachine as sm
    conn = lab.db()
    try:
        sm.claim_next_event(conn, "owner-a")
        conn.execute("UPDATE outbox SET status='ready', lease_expires_at=1")
        taken = sm.claim_next_event(conn, "owner-b")
    finally:
        conn.close()
    assert taken is not None and taken["lease_owner"] == "owner-b"


def test_readyz_degraded_when_only_index_down(lab_noworker, key):
    lab = lab_noworker
    lab.call("memory_commit", commit_args(key()))
    lab.mock.stop()
    from hippocampus.worker import Worker
    Worker(lab.env).process_one()
    status, body = lab.raw(b"", method="GET", url=lab.url.replace("/mcp/", "/readyz"))
    assert status == 200 and body["status"] == "degraded"
    assert body["reason"] == "index_backend_unavailable"
    assert set(body) == {"status", "reason"}  # no config/path/model/token


def test_malformed_success_recall_degrades_as_index_unavailable(lab_noworker):
    lab = lab_noworker
    lab.mock.invalid_json_next()
    _, error, is_error = lab.call(
        "memory_search", {"query": "Hippocampus", "project": "commissioning"})
    assert is_error and error["code"] == C.E_INDEX_UNAVAILABLE
    assert lab.env.index_backend_down
    status, body = lab.raw(b"", method="GET", url=lab.url.replace("/mcp/", "/readyz"))
    assert status == 200 and body == {
        "status": "degraded", "reason": "index_backend_unavailable"}


def test_malformed_success_retain_releases_worker_lease_for_retry(lab_noworker, key):
    lab = lab_noworker
    _, committed, is_error = lab.call("memory_commit", commit_args(key()))
    assert not is_error, committed
    lab.mock.invalid_json_next()
    from hippocampus.worker import Worker
    assert Worker(lab.env).process_one()

    conn = lab.db()
    try:
        row = dict(conn.execute(
            "SELECT status, error_code, attempt, lease_owner, lease_expires_at FROM outbox"
        ).fetchone())
    finally:
        conn.close()
    assert row == {
        "status": "retry_wait",
        "error_code": "UPSTREAM_5XX",
        "attempt": 1,
        "lease_owner": None,
        "lease_expires_at": None,
    }
    assert lab.env.index_backend_down and lab.mock.retain_log == []


def test_commit_latency_under_index_outage(lab_noworker, key):
    lab = lab_noworker
    lab.mock.stop()
    samples = []
    for _ in range(20):
        t0 = time.monotonic()
        _, data, is_error = lab.call("memory_commit", commit_args(key()))
        samples.append((time.monotonic() - t0) * 1000)
        assert not is_error and data["index_state"] == "index_pending"
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    assert p95 <= 500, f"p95={p95:.1f}ms exceeds the 500ms gate"


def test_orphan_vault_file_never_indexed(lab_noworker, key):
    lab = lab_noworker
    d = Path(lab.vault) / "shared" / "commissioning"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mem_20260808_ABCDEFGHJK.md").write_text("---\nid: x\n---\n", encoding="utf-8")
    from hippocampus.worker import Worker
    assert Worker(lab.env).process_one() is False
    assert lab.mock.retain_log == []

"""P0.7 + 09 §10.4: the fixed dataset, imported by three client identities."""

import json
import time
import uuid
from pathlib import Path

import pytest

DATASET = Path(__file__).resolve().parents[2] / "datasets" / "dataset.json"


@pytest.fixture(scope="module")
def dataset():
    if not DATASET.exists():
        pytest.skip("dataset.json not generated")
    return json.loads(DATASET.read_text(encoding="utf-8"))


@pytest.fixture
def loaded(lab, dataset):
    by_title = {}
    for rec in dataset["records"]:
        args = {
            "idempotency_key": str(uuid.uuid4()),
            "title": rec["title"], "summary": rec["summary"],
            "retrieval_text": rec["retrieval_text"], "project": "commissioning",
            "type": rec["type"], "detail_body": rec["detail_body"],
        }
        if rec["event_at"]:
            args["event_at"] = rec["event_at"]
        _, data, is_error = lab.call("memory_commit", args, client=rec["assigned_client"])
        assert not is_error, (rec["seq"], data)
        by_title[rec["title"]] = data
    assert lab.drain_worker(timeout=30)
    return by_title


def test_all_records_import_and_index(lab, dataset, loaded):
    assert len(loaded) == len(dataset["records"])
    conn = lab.db()
    try:
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) c FROM outbox GROUP BY status").fetchall())
    finally:
        conn.close()
    assert counts == {"indexed": len(dataset["records"])}


def test_retrieval_pipeline_over_ground_truth(lab, dataset, loaded):
    """Pipeline check against the fixed query set, NOT a semantic-quality gate.

    The mock scores by character-bigram overlap and has no embeddings or
    reranker, so recall quality (09 §10.4) stays open until the real Hindsight
    stack runs in P0.3/P0.6. What is verified here: every query returns a
    well-formed, correctly filtered, deduplicated, <=5-card result set, and the
    lexical proxy ranks the expected document into the top 5 for most queries.
    """
    misses, latencies = [], []
    for case in dataset["ground_truth"]:
        t0 = time.monotonic()
        _, result, is_error = lab.call(
            "memory_search", {"query": case["query"], "project": "commissioning"})
        latencies.append((time.monotonic() - t0) * 1000)
        assert not is_error, case["query"]
        cards = result["results"]
        assert len(cards) <= 5
        assert len({c["document_id"] for c in cards}) == len(cards)  # unique documents
        assert all(f"project:commissioning" in c["tags"] for c in cards)
        got = {c["index_title"] for c in cards}
        for expected in case["expected_titles"]:
            if expected not in got:
                misses.append((case["query"], expected))
    hit_rate = 1 - len(misses) / len(dataset["ground_truth"])
    assert hit_rate >= 0.8, f"lexical proxy hit rate {hit_rate:.0%}, misses={misses}"
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    assert p95 <= 5000, f"search p95={p95:.0f}ms exceeds the 5s target"


def test_event_time_anchored_not_import_time(lab, dataset, loaded):
    timed = [r for r in dataset["records"] if r["event_at"]]
    assert timed
    for rec in timed:
        doc = loaded[rec["title"]]["document_id"]
        _, item = next(x for x in lab.mock.retain_log if x[1]["document_id"] == doc)
        assert item["timestamp"] == rec["event_at"]
        assert item["metadata"]["event_at"] == rec["event_at"]
    untimed = [r for r in dataset["records"] if not r["event_at"]]
    for rec in untimed[:5]:
        doc = loaded[rec["title"]]["document_id"]
        _, item = next(x for x in lab.mock.retain_log if x[1]["document_id"] == doc)
        assert item["timestamp"] == "unset"  # never anchored to import time


def test_exact_string_recall(lab, dataset, loaded):
    for query, expected in [("192.168.2.41", None),
                            ("HIPPOCAMPUS_IDEMPOTENCY_CONFLICT", "幂等冲突判定"),
                            ("DURABILITY_GAP", "DURABILITY_GAP 处置"),
                            ("cl100k_base", "token 计量口径")]:
        _, result, is_error = lab.call(
            "memory_search", {"query": query, "project": "commissioning"})
        assert not is_error and result["results"], query
        if expected:
            assert expected in {c["index_title"] for c in result["results"]}, query


def test_cross_client_visibility_matrix(lab, dataset, loaded):
    claude_doc = next(r for r in dataset["records"] if r["assigned_client"] == "mac-claude")
    target = loaded[claude_doc["title"]]
    for reader in ("mac-codex", "dockerNode-openclaw"):
        _, read, is_error = lab.call("memory_read", {"uris": [target["uri"]]}, client=reader)
        assert not is_error, reader
        assert claude_doc["title"] in read["documents"][0]["markdown"]


def test_rebuild_from_frontmatter_only(lab, dataset, loaded):
    """09 §10.1: clearing the bank and reindexing from frontmatter alone."""
    from hippocampus import constants as C, vault
    from hippocampus.mdrender import parse_frontmatter
    lab.mock.banks.clear()
    conn = lab.db()
    try:
        conn.execute("UPDATE outbox SET status='ready', lease_owner=NULL, lease_expires_at=NULL")
    finally:
        conn.close()
    assert lab.drain_worker(timeout=30)
    assert len(lab.mock.banks[C.BANK_COMMISSIONING]) == len(dataset["records"])
    doc_id, item = next(iter(lab.mock.banks[C.BANK_COMMISSIONING].items()))
    project, _ = vault.parse_uri(item["metadata"]["uri"])
    md = vault.doc_path(lab.vault, project, doc_id).read_bytes()
    assert item["content"] == parse_frontmatter(md)["retrieval_text"]  # Detail never read

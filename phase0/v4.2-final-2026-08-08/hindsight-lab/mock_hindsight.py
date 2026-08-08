"""In-memory mock Hindsight for Phase 0 disposable verification (plan 09 §10.1/§10.3).

Implements just the two endpoints Hippocampus uses:
  POST /v1/default/banks/<bank>/memories          (retain, replace upsert)
  POST /v1/default/banks/<bank>/memories/recall   (recall with tag_groups all_strict)

Recall may split one document's content into multiple facts to exercise the
R12 merge path. Behavior knobs: fail_next(n), timeout_next(n).
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PATH = re.compile(r"^/v1/default/banks/([A-Za-z0-9._-]+)/memories(/recall)?$")


class MockHindsight:
    def __init__(self):
        self.banks: dict[str, dict[str, dict]] = {}
        self.retain_log: list[tuple[str, dict]] = []
        self._fail = 0
        self._timeout = 0
        self._lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None

    # knobs -----------------------------------------------------------------
    def fail_next(self, n: int = 1):
        with self._lock:
            self._fail = n

    def timeout_next(self, n: int = 1):
        with self._lock:
            self._timeout = n

    def clear(self):
        with self._lock:
            self.banks.clear()
            self.retain_log.clear()
            self._fail = self._timeout = 0

    # server ----------------------------------------------------------------
    def start(self) -> str:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                m = _PATH.match(self.path)
                if not m:
                    return self._reply(404, {"error": "not_found"})
                with mock._lock:
                    if mock._timeout > 0:
                        mock._timeout -= 1
                        time.sleep(3.0)
                        return self._reply(504, {"error": "timeout"})
                    if mock._fail > 0:
                        mock._fail -= 1
                        return self._reply(503, {"error": "injected"})
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                bank = m.group(1)
                if m.group(2):  # recall
                    return self._reply(200, mock._recall(bank, body))
                return self._reply(200, mock._retain(bank, body))

            def _reply(self, status, payload):
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    # semantics -------------------------------------------------------------
    def _retain(self, bank: str, body: dict) -> dict:
        assert body.get("async") is False
        items = body["items"]
        store = self.banks.setdefault(bank, {})
        for item in items:
            assert item["update_mode"] == "replace"
            store[item["document_id"]] = item  # replace: old fact set does not survive
            self.retain_log.append((bank, item))
        return {"stored": len(items)}

    def _recall(self, bank: str, body: dict) -> dict:
        query = body.get("query", "")
        groups = body.get("tag_groups", [])
        results = []
        for doc in self.banks.get(bank, {}).values():
            tags = set(doc.get("tags", []))
            if any(not set(g["tags"]).issubset(tags) for g in groups if g.get("match") == "all_strict"):
                continue
            content = doc["content"]
            score = _overlap(query, content)
            if score <= 0:
                continue
            halves = [content[: len(content) // 2], content[len(content) // 2:]]
            for i, part in enumerate(h for h in halves if h):
                # Field names mirror real Hindsight v0.9.0 recall: the fact body
                # is `text` and ranking lives under `scores.final`. Verified
                # against the deployed 0.9.0 API on 2026-08-08; keep in sync or
                # the pipeline tests validate a shape that does not exist.
                results.append({
                    "document_id": doc["document_id"],
                    "text": part,
                    "scores": {"final": score - i * 0.01, "semantic": score, "keyword": score},
                    "mentioned_at": "1999-01-01T00:00:00+00:00",  # must never reach a card
                    "metadata": doc["metadata"],
                    "tags": doc["tags"],
                })
        results.sort(key=lambda r: r["scores"]["final"], reverse=True)
        return {"results": results[:32]}


def _overlap(query: str, content: str) -> float:
    """Character-bigram lexical overlap.

    This is a LEXICAL PROXY ONLY. It has no embeddings, reranking, or query
    analysis, so it cannot stand in for Hindsight's semantic recall. Tests
    built on it verify the retrieval *pipeline* (strict tag filtering, per
    document_id merging, top-5 truncation, card shape) — recall *quality*
    must be measured against a real Hindsight instance (plan 08 Phase 0
    steps 3/6, acceptance 09 §10.4).
    """
    grams = _bigrams(query)
    if not grams:
        return 0.0
    hits = sum(1 for g in grams if g in content)
    return hits / len(grams)


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[\s,;:。，、?？!！]+", "", text)
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}

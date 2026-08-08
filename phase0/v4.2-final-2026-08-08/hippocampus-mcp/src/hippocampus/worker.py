"""Single-instance index worker (plan 05 §5.4)."""

from __future__ import annotations

import hashlib
import threading

from . import constants as C, failpoints, statemachine as sm, vault
from .db import connect, now
from .hindsight_client import HindsightError
from .mdrender import parse_frontmatter


class Worker:
    def __init__(self, env, poll_interval: float = 0.2):
        self.env = env
        self.poll = poll_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hippocampus-worker")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            worked = False
            if not self.env.scanner_down:
                try:
                    worked = self.process_one()
                except Exception:
                    worked = False
            if not worked:
                self._stop.wait(self.poll)

    def process_one(self) -> bool:
        env = self.env
        if env.scanner_down:
            return False
        conn = connect(env.db_path)
        try:
            claimed = sm.claim_next_event(conn, env.pid)
            if claimed is None:
                return False
            event_id = claimed["event_id"]
            attempt = claimed["attempt"]
            try:
                project, doc_id = vault.parse_uri(claimed["uri"])
                path = vault.doc_path(env.vault_root, project, doc_id)
                vault.refuse_symlink(path)
                if not path.exists():
                    sm.mark_durability_gap(conn, event_id)
                    env.health["durability_gap"] = True
                    return True
                data = path.read_bytes()
                if vault.sha256_bytes(data) != claimed["desired_sha256"]:
                    sm.set_outbox_status(conn, event_id, "conflict",
                                         error_code=C.E_HASH_MISMATCH, release_lease=True)
                    return True
                if env.scanner_down:
                    sm.set_outbox_status(conn, event_id, "ready", release_lease=True)
                    return False
                if env.scan_text(data.decode("utf-8")):
                    sm.set_outbox_status(conn, event_id, "policy_blocked",
                                         error_code=C.E_POLICY_BLOCKED, release_lease=True)
                    return True
                meta = parse_frontmatter(data)
                retrieval = meta["retrieval_text"]
                event_at = meta["event_at"]
                item = {
                    "document_id": doc_id,
                    "content": retrieval,
                    "update_mode": "replace",
                    "timestamp": event_at if event_at != "unset" else "unset",
                    "metadata": {
                        "uri": claimed["uri"],
                        "project": project,
                        "source_agent": meta["source_agent"],
                        "event_at": event_at,
                        "index_title": meta["title"],
                        "index_summary": meta["summary"],
                        "trust": meta["trust"],
                        "sensitivity": meta["sensitivity"],
                        "retrieval_sha256": hashlib.sha256(retrieval.encode("utf-8")).hexdigest(),
                    },
                    "tags": meta["tags"],
                }
                retain_texts = [item["content"], item["metadata"]["index_title"],
                                item["metadata"]["index_summary"]]
                if any(env.scan_text(t) for t in retain_texts):
                    sm.set_outbox_status(conn, event_id, "policy_blocked",
                                         error_code=C.E_POLICY_BLOCKED, release_lease=True)
                    return True
                failpoints.hit("worker.retain.before")
                env.hindsight.retain(claimed["server_bank_id"], item)
                failpoints.hit("worker.retain.after")
                sm.set_outbox_status(conn, event_id, "indexed", release_lease=True)
                env.index_backend_down = False
                return True
            except HindsightError as exc:
                env.index_backend_down = True
                if attempt >= C.MAX_ATTEMPTS:
                    sm.set_outbox_status(conn, event_id, "dead", error_code=exc.code,
                                         release_lease=True)
                else:
                    delay = min(env.backoff_base * (2 ** (attempt - 1)), C.BACKOFF_CAP_S)
                    sm.set_outbox_status(conn, event_id, "retry_wait", error_code=exc.code,
                                         next_attempt_at=now() + max(int(delay), 0),
                                         release_lease=True)
                return True
        finally:
            conn.close()

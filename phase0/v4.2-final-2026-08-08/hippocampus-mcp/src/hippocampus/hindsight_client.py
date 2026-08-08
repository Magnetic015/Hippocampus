"""Minimal Hindsight HTTP client (plan 03 §3.4, 07 §7.1). Mock-compatible."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request


class HindsightError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# Retain runs LLM fact extraction inside Hindsight and routinely takes tens of
# seconds; recall is interactive and must stay short. A single short timeout
# makes the worker abandon in-flight retains and re-run the extraction on every
# attempt, so the two are configured separately.
DEFAULT_RETAIN_TIMEOUT_S = 300.0
DEFAULT_RECALL_TIMEOUT_S = 15.0


class HindsightClient:
    def __init__(self, base_url: str, token: str,
                 retain_timeout: float = DEFAULT_RETAIN_TIMEOUT_S,
                 recall_timeout: float = DEFAULT_RECALL_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.retain_timeout = retain_timeout
        self.recall_timeout = recall_timeout

    def _post(self, path: str, payload: dict, timeout: float) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise HindsightError("UPSTREAM_5XX" if exc.code >= 500 else "UPSTREAM_4XX") from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
            raise HindsightError("HINDSIGHT_TIMEOUT") from None

    def retain(self, bank: str, item: dict) -> dict:
        return self._post(f"/v1/default/banks/{bank}/memories",
                          {"items": [item], "async": False}, self.retain_timeout)

    def recall(self, bank: str, query: str, tag_groups: list[dict], *, budget: str, max_tokens: int) -> dict:
        return self._post(
            f"/v1/default/banks/{bank}/memories/recall",
            {"query": query, "budget": budget, "max_tokens": max_tokens, "tag_groups": tag_groups},
            self.recall_timeout,
        )

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


class HindsightClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise HindsightError("UPSTREAM_5XX" if exc.code >= 500 else "UPSTREAM_4XX") from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
            raise HindsightError("HINDSIGHT_TIMEOUT") from None

    def retain(self, bank: str, item: dict) -> dict:
        return self._post(f"/v1/default/banks/{bank}/memories", {"items": [item], "async": False})

    def recall(self, bank: str, query: str, tag_groups: list[dict], *, budget: str, max_tokens: int) -> dict:
        return self._post(
            f"/v1/default/banks/{bank}/memories/recall",
            {"query": query, "budget": budget, "max_tokens": max_tokens, "tag_groups": tag_groups},
        )

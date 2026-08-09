"""Text measurement (F5): Unicode scalar counts + version-locked cl100k_base tokens."""

from __future__ import annotations

import os
from pathlib import Path

_ENCODING = None


def scalar_len(text: str) -> int:
    return len(text)


def _vendor_cache_dir() -> str:
    env = os.environ.get("HIPPOCAMPUS_TIKTOKEN_CACHE")
    if env:
        return env
    return str(Path(__file__).resolve().parents[2] / "vendor" / "tiktoken-cache")


def token_count(text: str) -> int:
    """cl100k_base token count via pinned tiktoken; raises if unavailable."""
    global _ENCODING
    if _ENCODING is None:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", _vendor_cache_dir())
        import tiktoken

        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return len(_ENCODING.encode(text, disallowed_special=()))

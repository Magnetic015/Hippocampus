"""Markdown render/parse for the fixed frontmatter schema (plan 03 §3.3).

The renderer is the only writer, so the parser only needs to understand the
exact shape the renderer emits: `key: <json string>` single-line values, a
`retrieval_text: |-` literal block, and a JSON array for tags.
"""

from __future__ import annotations

import json

_ORDER = ("id", "title", "summary")
_TAIL = ("uri", "project", "source_agent", "scope", "trust", "sensitivity", "type",
         "created_at", "updated_at", "event_at")


def render(meta: dict, retrieval_text: str, detail_body: str | None) -> bytes:
    lines = ["---"]
    for key in _ORDER:
        lines.append(f"{key}: {json.dumps(meta[key], ensure_ascii=False)}")
    lines.append("retrieval_text: |-")
    for raw in retrieval_text.splitlines() or [""]:
        lines.append(f"  {raw}")
    for key in _TAIL:
        lines.append(f"{key}: {json.dumps(meta[key], ensure_ascii=False)}")
    lines.append(f"tags: {json.dumps(meta['tags'], ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append("## Detail")
    lines.append("")
    lines.append(detail_body or "")
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_frontmatter(data: bytes) -> dict:
    text = data.decode("utf-8")
    lines = text.split("\n")
    if lines[0] != "---":
        raise ValueError("missing frontmatter")
    meta: dict = {}
    i = 1
    while i < len(lines) and lines[i] != "---":
        line = lines[i]
        if line == "retrieval_text: |-":
            block: list[str] = []
            i += 1
            while i < len(lines) and lines[i].startswith("  "):
                block.append(lines[i][2:])
                i += 1
            meta["retrieval_text"] = "\n".join(block)
            continue
        key, _, rest = line.partition(": ")
        meta[key] = json.loads(rest)
        i += 1
    return meta

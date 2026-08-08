"""Bounded, log-free JSON-RPC ingress parser (plan 07 §7.0, R7).

Raw method/id/tool-name/unknown-key values are only used for rejection
decisions inside this module and are never persisted or echoed.
"""

from __future__ import annotations

import json

from .constants import (
    MAX_ARRAY_ITEMS,
    MAX_ENVELOPE_STR_BYTES,
    MAX_JSON_DEPTH,
    MAX_OBJECT_FIELDS,
    OPERATIONS,
    TOOLS,
)


class IngressError(Exception):
    def __init__(self, reason: str, field_path: str = "unknown_field"):
        super().__init__(reason)
        self.reason = reason
        self.field_path = field_path


_ENVELOPE_KEYS = {"jsonrpc", "id", "method", "params"}


def _pairs_hook(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise IngressError("duplicate_key")
        seen.add(key)
    return dict(pairs)


def _check_bounds(node, depth: int = 1) -> None:
    if depth > MAX_JSON_DEPTH:
        raise IngressError("depth_exceeded")
    if isinstance(node, dict):
        if len(node) > MAX_OBJECT_FIELDS:
            raise IngressError("too_many_fields")
        for value in node.values():
            _check_bounds(value, depth + 1)
    elif isinstance(node, list):
        if len(node) > MAX_ARRAY_ITEMS:
            raise IngressError("too_many_items")
        for value in node:
            _check_bounds(value, depth + 1)


def parse_envelope(raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise IngressError("bad_encoding") from None
    decoder = json.JSONDecoder(object_pairs_hook=_pairs_hook)
    try:
        obj, end = decoder.raw_decode(text)
    except json.JSONDecodeError:
        raise IngressError("bad_json") from None
    if text[end:].strip():
        raise IngressError("trailing_data")
    if isinstance(obj, list):
        raise IngressError("batch_rejected")
    if not isinstance(obj, dict):
        raise IngressError("not_object")
    _check_bounds(obj)
    unknown = set(obj) - _ENVELOPE_KEYS
    if unknown:
        raise IngressError("unknown_envelope_key")
    if obj.get("jsonrpc") != "2.0":
        raise IngressError("bad_jsonrpc")
    method = obj.get("method")
    if not isinstance(method, str) or not method:
        raise IngressError("bad_method", "jsonrpc_method")
    if len(method.encode("utf-8")) > MAX_ENVELOPE_STR_BYTES:
        raise IngressError("method_too_long", "jsonrpc_method")
    rid = obj.get("id")
    if rid is not None and not isinstance(rid, (str, int)):
        raise IngressError("bad_id", "jsonrpc_id")
    if isinstance(rid, str) and len(rid.encode("utf-8")) > MAX_ENVELOPE_STR_BYTES:
        raise IngressError("id_too_long", "jsonrpc_id")
    params = obj.get("params")
    if params is not None and not isinstance(params, dict):
        raise IngressError("bad_params")
    return {"id": rid, "method": method, "params": params or {}}


_METHOD_MAP = {
    "initialize": "initialize",
    "notifications/initialized": "initialized",
    "tools/list": "tools_list",
    "tools/call": "tools_call",
}


def map_operation(method: str) -> str | None:
    op = _METHOD_MAP.get(method)
    return op if op in OPERATIONS or op is None else None


def map_tool(name) -> tuple[str, str | None]:
    """Returns (tool_enum, raw_name_if_safe). Unknown/malformed -> invalid_tool."""
    if isinstance(name, str) and len(name.encode("utf-8")) <= MAX_ENVELOPE_STR_BYTES and name in TOOLS:
        return name, name
    return "invalid_tool", None


def envelope_scan_texts(envelope: dict) -> list[str]:
    texts = [envelope["method"]]
    if isinstance(envelope["id"], str):
        texts.append(envelope["id"])
    name = envelope["params"].get("name")
    if isinstance(name, str):
        texts.append(name)
    return texts

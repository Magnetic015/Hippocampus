"""P0.10 / 09 §10.3 last bullet: JSON-RPC ingress gate."""

import gzip
import json

import pytest
from harness import CLIENTS, commit_args

from hippocampus import constants as C


def test_batch_rejected_wholesale(lab):
    body = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}] * 2).encode()
    status, payload = lab.raw(body, token=CLIENTS["mac-claude"])
    assert status == 400 and payload["error"]["code"] == -32600
    assert payload["id"] is None
    rows = lab.audit_rows()
    assert len(rows) == 1 and rows[0]["state"] == "rejected"  # never split into pseudo-calls


def test_compressed_body_rejected(lab):
    raw = gzip.compress(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode())
    status, _ = lab.raw(raw, token=CLIENTS["mac-claude"], headers={"Content-Encoding": "gzip"})
    assert status == 415


def test_unknown_content_type_rejected(lab):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    status, _ = lab.raw(body, token=CLIENTS["mac-claude"], headers={"Content-Type": "text/plain"})
    assert status == 415


def test_oversize_body_rejected(lab):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                       "params": {"pad": "x" * (C.MAX_BODY_BYTES + 10)}}).encode()
    status, _ = lab.raw(body, token=CLIENTS["mac-claude"])
    assert status == 413


def test_duplicate_key_and_trailing_data_rejected(lab):
    dup = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","method":"initialize"}'
    assert lab.raw(dup, token=CLIENTS["mac-claude"])[0] == 400
    trailing = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"} {"x":1}'
    assert lab.raw(trailing, token=CLIENTS["mac-claude"])[0] == 400


def test_depth_and_field_limits(lab):
    deep = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}
    node = deep["params"]
    for _ in range(C.MAX_JSON_DEPTH + 2):
        node["n"] = {}
        node = node["n"]
    assert lab.raw(json.dumps(deep).encode(), token=CLIENTS["mac-claude"])[0] == 400

    wide = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {f"k{i}": 1 for i in range(C.MAX_OBJECT_FIELDS + 1)}}
    assert lab.raw(json.dumps(wide).encode(), token=CLIENTS["mac-claude"])[0] == 400


def test_envelope_string_length_limit(lab):
    long_id = "x" * (C.MAX_ENVELOPE_STR_BYTES + 1)
    status, _ = lab.rpc("tools/list", rpc_id=long_id)
    assert status == 400


@pytest.mark.parametrize("canary_field,body", [
    ("method", {"jsonrpc": "2.0", "id": 1, "method": "AKIACANARY0EXAMPLE99"}),
    ("id", {"jsonrpc": "2.0", "id": "sk-canary0123456789ABCDEFghijkl", "method": "tools/list"}),
    ("tool_name", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "ghp_Canary0123456789abcdefghij", "arguments": {}}}),
])
def test_envelope_canaries_rejected_without_echo(lab, canary_field, body):
    status, payload = lab.raw(json.dumps(body).encode(), token=CLIENTS["mac-claude"])
    assert status in (400, 404)
    text = json.dumps(payload, ensure_ascii=False)
    for needle in ("AKIA", "sk-canary", "ghp_Canary"):
        assert needle not in text
    assert payload["id"] is None
    rows = lab.audit_rows()
    assert len(rows) == 1
    row_text = json.dumps(rows[0], ensure_ascii=False)
    for needle in ("AKIA", "sk-canary", "ghp_Canary"):
        assert needle not in row_text


def test_unknown_tool_recorded_as_invalid_tool(lab):
    status, payload = lab.rpc("tools/call", {"name": "memory_delete_everything", "arguments": {}})
    assert status == 400
    rows = lab.audit_rows()
    assert rows[0]["tool_enum"] == C.INVALID_TOOL
    assert "memory_delete_everything" not in json.dumps(rows[0], ensure_ascii=False)


def test_unknown_argument_key_recorded_as_unknown_field(lab, key):
    args = commit_args(key())
    args["surprise_key"] = "value"
    status, data, is_error = lab.call("memory_commit", args)
    assert is_error and data["code"] == C.E_SCHEMA_REJECTED
    assert data["fields"] == [C.UNKNOWN_FIELD]


def test_lifecycle_methods_require_auth(lab):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
    assert lab.raw(body)[0] == 401
    assert lab.rpc("initialize")[0] == 200
    assert lab.rpc("tools/list")[0] == 200


def test_tools_list_exposes_exactly_four_tools(lab):
    _, payload = lab.rpc("tools/list")
    names = {t["name"] for t in payload["result"]["tools"]}
    assert names == set(C.TOOLS)

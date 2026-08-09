"""P1.4/P1.5/P2.x and 09 §10.6: identity, union gate, peer binding, authorization."""

import json

from harness import CLIENTS, PEPPER, commit_args

from hippocampus import constants as C, registry
from hippocampus.db import now
from hippocampus.registry import AuthzError
from hippocampus.server import Env, build_server


def test_no_token_is_401_and_writes_unauth_row(lab):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    assert lab.raw(body)[0] == 401
    rows = lab.audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["client_id"] is None and row["source_ip"] == "127.0.0.1"
    assert row["state"] == "rejected" and row["outcome_code"] == C.OUTCOME_AUTH_FAILED


def test_wrong_token_never_recorded(lab):
    status, _ = lab.rpc("tools/list", token="Tbogus" + "z" * 40)
    assert status == 401
    rows = lab.audit_rows()
    assert "bogus" not in json.dumps(rows, ensure_ascii=False)


def test_authenticated_rows_have_no_source_ip(lab):
    lab.rpc("tools/list")
    row = lab.audit_rows()[0]
    assert row["client_id"] == "mac-claude" and row["source_ip"] is None


def test_revoked_token_rejected_others_unaffected(lab):
    conn = lab.db()
    try:
        registry.revoke_client(conn, "mac-claude")
    finally:
        conn.close()
    assert lab.rpc("tools/list", client="mac-claude")[0] == 401
    assert lab.rpc("tools/list", client="mac-codex")[0] == 200


def test_expired_token_rejected(lab):
    conn = lab.db()
    try:
        conn.execute("UPDATE clients SET expires_at=1 WHERE client_id='mac-codex'")
    finally:
        conn.close()
    assert lab.rpc("tools/list", client="mac-codex")[0] == 401


def test_rotation_old_token_fails_new_succeeds(lab):
    new_token = "T" + "rotated" * 8
    conn = lab.db()
    try:
        registry.rotate_token(conn, "mac-codex", new_token, PEPPER)
    finally:
        conn.close()
    assert lab.rpc("tools/list", token=CLIENTS["mac-codex"])[0] == 401
    assert lab.rpc("tools/list", token=new_token)[0] == 200


def test_rotation_renews_finite_expiry_but_preserves_permanent_clients(lab):
    finite_token = "T" + "finite-rotated" * 4
    permanent_token = "T" + "permanent-rotated" * 4
    conn = lab.db()
    try:
        conn.execute("UPDATE clients SET expires_at=1 WHERE client_id='mac-codex'")
        registry.rotate_token(
            conn, "mac-codex", finite_token, PEPPER, renew_expires_days=14)
        registry.rotate_token(
            conn, "mac-claude", permanent_token, PEPPER, renew_expires_days=14)
        rows = {
            row["client_id"]: row["expires_at"]
            for row in conn.execute(
                "SELECT client_id, expires_at FROM clients"
                " WHERE client_id IN ('mac-codex','mac-claude')"
            )
        }
        assert rows["mac-codex"] >= now() + 13 * 86400
        assert rows["mac-claude"] is None
    finally:
        conn.close()

    assert lab.rpc("tools/list", token=finite_token)[0] == 200
    assert lab.rpc("tools/list", token=permanent_token)[0] == 200


def test_peer_revocation_causes_gate_denial(lab):
    conn = lab.db()
    try:
        for cid in CLIENTS:
            registry.revoke_peer(conn, cid, "127.0.0.1")
    finally:
        conn.close()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    status, _ = lab.raw(body, token=CLIENTS["mac-claude"])
    assert status == 403
    row = lab.audit_rows()[0]
    assert row["outcome_code"] == C.OUTCOME_SOURCE_DENIED and row["client_id"] is None


def test_client_peer_mismatch_denied_even_inside_gate(lab):
    """mac-codex keeps 127.0.0.1 in the union gate; mac-claude loses its binding."""
    conn = lab.db()
    try:
        registry.revoke_peer(conn, "mac-claude", "127.0.0.1")
    finally:
        conn.close()
    assert lab.rpc("tools/list", client="mac-codex")[0] == 200
    status, _ = lab.rpc("tools/list", client="mac-claude")
    assert status == 403
    mismatch = [r for r in lab.audit_rows() if r["outcome_code"] == C.OUTCOME_SOURCE_MISMATCH]
    assert len(mismatch) == 1 and mismatch[0]["client_id"] == "mac-claude"


def test_empty_gate_refuses_to_start(tmp_path):
    from hippocampus.db import init_db
    db = str(tmp_path / "state" / "outbox.db")
    init_db(db)
    env = Env(db_path=db, vault_root=str(tmp_path / "vault"), mode=C.MODE_COMMISSIONING,
              bind=("127.0.0.1", 0), hindsight_base="http://127.0.0.1:1",
              hindsight_token="x", token_pepper=PEPPER, audit_key=b"a", idem_key=b"i")
    try:
        build_server(env)
        raise AssertionError("server started with an empty gate")
    except SystemExit as exc:
        assert "empty source union gate" in str(exc)


def test_unauthorized_project_denied_for_all_tools(lab, key):
    status, data, is_error = lab.call("memory_search", {"query": "写入链路", "project": "global"})
    assert is_error and data["code"] == C.E_AUTHZ_DENIED
    status, data, is_error = lab.call("memory_commit", commit_args(key(), project="global"))
    assert is_error and data["code"] == C.E_AUTHZ_DENIED


def test_global_requires_explicit_grant(tmp_path):
    from harness import Lab
    lab = Lab(tmp_path, readable=("commissioning", "global"), writable=("commissioning",))
    try:
        _, data, is_error = lab.call("memory_search", {"query": "任意", "project": "global"})
        assert not is_error  # explicit readable grant honoured
        _, data, is_error = lab.call("memory_commit",
                                     commit_args("2f1c1f6e-3b2a-4a6d-9c1e-4d5b6a7c8d90",
                                                 project="global"))
        assert is_error and data["code"] == C.E_AUTHZ_DENIED  # write not granted
    finally:
        lab.close()


def test_reserved_fields_rejected_not_overwritten(lab, key):
    for field, value in [("uri", "memory://shared/commissioning/mem_20260808_0001"),
                         ("document_id", "mem_20260808_0001"), ("trust", "curated"),
                         ("tags", "x"), ("scope", "private"), ("sensitivity", "public")]:
        args = commit_args(key())
        args[field] = value
        _, data, is_error = lab.call("memory_commit", args)
        assert is_error and data["code"] == C.E_RESERVED_FIELD, field


def test_filter_cannot_widen_permissions(lab):
    _, data, is_error = lab.call("memory_search",
                                 {"query": "写入", "project": "commissioning", "source": "nope"})
    assert is_error and data["code"] == C.E_SCHEMA_REJECTED


def test_authorize_helper_separates_read_and_write():
    row = {"readable_projects": json.dumps(["a"]), "writable_projects": json.dumps([]),
           "allowed_types": json.dumps(["fact"])}
    registry.authorize(row, action="read", project="a")
    for bad in (dict(action="write", project="a"), dict(action="read", project="b")):
        try:
            registry.authorize(row, **bad)
            raise AssertionError("expected denial")
        except AuthzError:
            pass


def test_registry_cli_never_prints_secrets(tmp_path, capsys, monkeypatch):
    from hippocampus import registry_cli
    monkeypatch.setenv("HIPPOCAMPUS_TOKEN_PEPPER", "pepper-value")
    db = str(tmp_path / "cli.db")
    token_file = tmp_path / "tok"
    token_file.write_text("T" + "s" * 60)
    token_file.chmod(0o600)
    registry_cli.main(["--db", db, "issue", "c1", "--source-tag", "c1",
                       "--readable", "p", "--writable", "p", "--token-file", str(token_file)])
    registry_cli.main(["--db", db, "bind-peer", "c1", "192.168.2.9"])
    registry_cli.main(["--db", db, "list-safe"])
    out = capsys.readouterr().out
    assert "T" + "s" * 60 not in out and "token_hash" not in out
    assert "192.168.2.9" in out


def test_registry_cli_rotation_renews_expired_finite_client(tmp_path, monkeypatch):
    from hippocampus import registry_cli
    monkeypatch.setenv("HIPPOCAMPUS_TOKEN_PEPPER", "pepper-value")
    db = str(tmp_path / "renew.db")
    token_file = tmp_path / "tok"
    token_file.write_text("T" + "initial" * 9)
    token_file.chmod(0o600)
    registry_cli.main([
        "--db", db, "issue", "finite-client", "--source-tag", "finite-client",
        "--readable", "p", "--writable", "p", "--expires-days", "1",
        "--token-file", str(token_file),
    ])
    conn = registry_cli.connect(db)
    try:
        conn.execute("UPDATE clients SET expires_at=1 WHERE client_id='finite-client'")
    finally:
        conn.close()

    token_file.write_text("T" + "rotated" * 9)
    registry_cli.main([
        "--db", db, "rotate", "finite-client", "--renew-expires-days", "14",
        "--token-file", str(token_file),
    ])
    conn = registry_cli.connect(db)
    try:
        row = conn.execute(
            "SELECT expires_at FROM clients WHERE client_id='finite-client'").fetchone()
        assert row["expires_at"] >= now() + 13 * 86400
    finally:
        conn.close()


def test_registry_cli_rejects_cidr_and_short_token(tmp_path, monkeypatch):
    from hippocampus import registry_cli
    monkeypatch.setenv("HIPPOCAMPUS_TOKEN_PEPPER", "pepper-value")
    db = str(tmp_path / "cli2.db")
    tok = tmp_path / "t"
    tok.write_text("short")
    tok.chmod(0o600)
    try:
        registry_cli.main(["--db", db, "issue", "c", "--source-tag", "c", "--token-file", str(tok)])
        raise AssertionError("short token accepted")
    except SystemExit:
        pass
    tok.write_text("T" + "s" * 60)
    registry_cli.main(["--db", db, "issue", "c", "--source-tag", "c", "--token-file", str(tok)])
    try:
        registry_cli.main(["--db", db, "bind-peer", "c", "192.168.2.0/24"])
        raise AssertionError("CIDR accepted")
    except ValueError:
        pass

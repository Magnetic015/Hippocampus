"""Regression coverage for the server/deployment findings from PR #1 review."""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest
from harness import CLIENTS, PEPPER, commit_args

from hippocampus import constants as C, failpoints, registry, server
from hippocampus.db import connect, init_db


ROOT = Path(__file__).resolve().parents[2]
MCP = ROOT / "hippocampus-mcp"
TEMPLATES = ROOT / "templates"


@pytest.fixture(autouse=True)
def _clear_failpoints():
    failpoints.clear()
    yield
    failpoints.clear()


def test_clean_checkout_docker_inputs_are_explicit_and_generated():
    dockerfile = (MCP / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY hippocampus-mcp/pyproject.toml ./" in dockerfile
    assert "COPY hippocampus-mcp/src ./src" in dockerfile
    assert "COPY scanner ./scanner" in dockerfile
    assert "COPY vendor" not in dockerfile and "scanner-pkg" not in dockerfile
    assert 'tiktoken.get_encoding("cl100k_base")' in dockerfile
    for source in (MCP / "pyproject.toml", MCP / "src", ROOT / "scanner"):
        assert source.exists()

    deployment = (ROOT.parents[1] / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "docker build -f hippocampus-mcp/Dockerfile --target production" in deployment


def test_mcp_compose_uses_tag_and_python_healthcheck():
    compose = (TEMPLATES / "compose.yaml").read_text(encoding="utf-8")
    mcp_service = compose.split("  hippocampus-mcp:\n", 1)[1]
    assert "hippocampus-mcp:${HIPPOCAMPUS_MCP_TAG:?required}" in mcp_service
    assert "HIPPOCAMPUS_MCP_DIGEST" not in compose
    assert "urllib.request.urlopen" in mcp_service
    assert "curl " not in mcp_service

    env_example = (TEMPLATES / "hippocampus.env.example").read_text(encoding="utf-8")
    assert "HIPPOCAMPUS_MCP_TAG=" in env_example
    assert "HIPPOCAMPUS_MCP_DIGEST=" not in env_example


def test_client_rollback_requires_private_manifest_and_matching_digest(tmp_path):
    script = TEMPLATES / "scripts" / "rollback-clients.sh"
    backup = tmp_path / "client.backup"
    target = tmp_path / "client.config"
    manifest = tmp_path / "rollback.tsv"
    trusted = b"known-good-client-config\n"
    digest = hashlib.sha256(trusted).hexdigest()

    backup.write_bytes(trusted)
    backup.chmod(0o600)
    target.write_bytes(b"current-config\n")
    manifest.write_text(
        f"client-a\tbackup-a\t{backup}\t{target}\t{digest}\n", encoding="utf-8")

    manifest.chmod(0o644)
    bad_mode = subprocess.run(
        ["bash", str(script), "--record", str(manifest), "--execute"],
        capture_output=True, text=True, check=False)
    assert bad_mode.returncode != 0 and "mode 0600" in bad_mode.stderr

    manifest.chmod(0o600)
    backup.write_bytes(b"modified-after-manifest\n")
    mismatch = subprocess.run(
        ["bash", str(script), "--record", str(manifest), "--execute"],
        capture_output=True, text=True, check=False)
    assert mismatch.returncode == 0
    assert "refused_digest_mismatch" in mismatch.stdout
    assert target.read_bytes() == b"current-config\n"
    assert digest not in mismatch.stdout + mismatch.stderr

    backup.write_bytes(trusted)
    restored = subprocess.run(
        ["bash", str(script), "--record", str(manifest), "--execute"],
        capture_output=True, text=True, check=False)
    assert restored.returncode == 0 and "result=restored" in restored.stdout
    assert target.read_bytes() == trusted
    assert digest not in restored.stdout + restored.stderr


def test_client_rollback_discards_failed_gnu_stat_probe_output(tmp_path):
    script = TEMPLATES / "scripts" / "rollback-clients.sh"
    backup = tmp_path / "client.backup"
    target = tmp_path / "client.config"
    manifest = tmp_path / "rollback.tsv"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == -f ]]; then printf 'gnu-filesystem-status\\n'; exit 1; fi\n"
        "if [[ $1 == -c && $2 == %a ]]; then printf '600\\n'; exit 0; fi\n"
        "if [[ $1 == -c && $2 == %y ]]; then "
        "printf '2026-08-09 12:00:00.000000000 +0800\\n'; exit 0; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)
    trusted = b"known-good-client-config\n"
    digest = hashlib.sha256(trusted).hexdigest()
    backup.write_bytes(trusted)
    backup.chmod(0o600)
    target.write_bytes(b"current-config\n")
    manifest.write_text(
        f"client-a\tbackup-a\t{backup}\t{target}\t{digest}\n", encoding="utf-8")
    manifest.chmod(0o600)

    result = subprocess.run(
        ["bash", str(script), "--record", str(manifest), "--execute"],
        capture_output=True, text=True, check=False,
        env=dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}"),
    )

    assert result.returncode == 0, result.stderr
    assert "result=restored" in result.stdout
    assert "gnu-filesystem-status" not in result.stdout + result.stderr
    assert target.read_bytes() == trusted


def test_client_deploy_probe_keeps_bearer_token_out_of_argv():
    script = (ROOT.parents[1] / "deploy-hippocampus-client.sh").read_text(encoding="utf-8")
    auth_ok = script.split("auth_ok(){", 1)[1].split("\n}", 1)[0]
    assert 'Authorization: Bearer $(cat' not in auth_ok
    assert "curl --config -" in auth_ok
    assert 'printf \'header = "Authorization: Bearer %s"' in auth_ok


def test_client_installers_align_fresh_grants_with_mode_and_expire_new_clients():
    repo = ROOT.parents[1]
    shell = (repo / "deploy-hippocampus-client.sh").read_text(encoding="utf-8")
    powershell = (repo / "deploy-hippocampus-client.ps1").read_text(encoding="utf-8")

    assert 'PROJECT="${PROJECT:-auto}"' in shell
    assert '[string]$Project = "auto"' in powershell
    for script in (shell, powershell):
        assert 'HIPPOCAMPUS_MODE:-commissioning' in script
        assert '[ "$PROJ" = auto ] && PROJ=commissioning' in script
        assert '[ "$PROJ" = auto ] && PROJ=soul' in script
        assert 'commissioning mode requires project=commissioning' in script

        issue = next(line for line in script.splitlines()
                     if "registry_cli" in line and " issue " in line)
        rotate = next(line for line in script.splitlines()
                      if "registry_cli" in line and " rotate " in line)
        assert '--expires-days "$EXPIRES_DAYS"' in issue
        assert '--renew-expires-days "$EXPIRES_DAYS"' in rotate


@pytest.mark.parametrize("mode,expected_project", [
    ("commissioning", "commissioning"),
    ("production", "soul"),
])
def test_shell_installer_fresh_issue_uses_runtime_mode_project_and_expiry(
        tmp_path, mode, expected_project):
    script = (ROOT.parents[1] / "deploy-hippocampus-client.sh").read_text(encoding="utf-8")
    remote = script.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE", 1)[0]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \" $* \" == *\" sh -c \"* ]]; then printf '%s' \"$FAKE_MODE\"; exit 0; fi\n"
        "if [[ \" $* \" == *\" rotate \"* ]]; then exit 1; fi\n"
        "if [[ \" $* \" == *\" issue \"* ]]; then printf '%s\\n' \"$*\" >\"$FAKE_LOG\"; cat >/dev/null; exit 0; fi\n"
        "if [[ \" $* \" == *\" bind-peer \"* ]]; then exit 0; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    log = tmp_path / "docker.log"
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}",
               FAKE_MODE=mode, FAKE_LOG=str(log))

    result = subprocess.run(
        ["bash", "-s", "--", "fresh-client", "mcp", "db", "192.0.2.4", "auto", "14"],
        input=remote, text=True, capture_output=True, env=env, check=False,
    )

    assert result.returncode == 0, result.stderr
    issue_args = log.read_text(encoding="utf-8")
    assert f"--readable {expected_project} --writable {expected_project}" in issue_args
    assert "--expires-days 14" in issue_args


def test_shell_installer_rejects_project_incompatible_with_commissioning(tmp_path):
    script = (ROOT.parents[1] / "deploy-hippocampus-client.sh").read_text(encoding="utf-8")
    remote = script.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE", 1)[0]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \" $* \" == *\" sh -c \"* ]]; then printf commissioning; exit 0; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")

    result = subprocess.run(
        ["bash", "-s", "--", "fresh-client", "mcp", "db", "192.0.2.4", "soul", "14"],
        input=remote, text=True, capture_output=True, env=env, check=False,
    )

    assert result.returncode == 2
    assert "commissioning mode requires project=commissioning" in result.stderr


def test_windows_installer_uses_process_scoped_launcher_not_plaintext_user_env():
    script = (ROOT.parents[1] / "deploy-hippocampus-client.ps1").read_text(encoding="utf-8")

    assert 'SetEnvironmentVariable($TokenEnv, $token, "User")' not in script
    assert 'Set-Item -Path "Env:$TokenEnv"' not in script
    assert 'SetEnvironmentVariable($TokenEnv, $null, "User")' in script
    assert 'SetEnvironmentVariable(`$tokenEnv, `$token, "Process")' in script
    assert 'Start-Process -FilePath `$CodexExecutable' in script
    assert "Write-CodexLauncher" in script
    assert 'bearer_token_env_var = "$TokenEnv"' in script


def test_revoke_peer_canonicalizes_ipv6_and_rejects_zero_updates(tmp_path):
    db_path = str(tmp_path / "registry.db")
    init_db(db_path)
    conn = connect(db_path)
    try:
        registry.create_client(
            conn, "client-a", "token", PEPPER, source_tag="client-a",
            readable=["p"], writable=["p"], types=list(C.TYPES))
        registry.bind_peer(conn, "client-a", "2001:0db8::1")
        assert conn.execute(
            "SELECT canonical_ip FROM client_sources").fetchone()["canonical_ip"] == "2001:db8::1"

        registry.revoke_peer(conn, "client-a", "2001:0db8::1")
        assert conn.execute(
            "SELECT revoked_at FROM client_sources").fetchone()["revoked_at"] is not None
        with pytest.raises(registry.AuthzError, match="NO_SUCH_PEER"):
            registry.revoke_peer(conn, "client-a", "2001:db8::2")
    finally:
        conn.close()


def test_source_tags_refresh_after_registry_change(lab_noworker):
    assert "new-source" not in lab_noworker.env.known_source_tags()
    conn = lab_noworker.db()
    try:
        registry.create_client(
            conn, "new-client", "new-token", PEPPER, source_tag="new-source",
            readable=["commissioning"], writable=["commissioning"], types=list(C.TYPES))
    finally:
        conn.close()
    assert "new-source" in lab_noworker.env.known_source_tags()


def test_idempotency_conflict_propagates_http_409(lab_noworker, key):
    idem_key = key()
    lab_noworker.call("memory_commit", commit_args(idem_key))
    status, data, is_error = lab_noworker.call(
        "memory_commit", commit_args(idem_key, title="different-title"))
    assert status == 409
    assert is_error and data["code"] == C.E_IDEMPOTENCY_CONFLICT


def test_rejected_tool_fails_closed_when_audit_finalization_fails(lab_noworker):
    failpoints.arm("audit.finalize", sqlite3.OperationalError("disk I/O error"))
    status, data, is_error = lab_noworker.call(
        "memory_search", {"query": "x", "project": "commissioning", "bogus": True})
    assert status == 200
    assert is_error and data["code"] == C.E_AUDIT_UNAVAILABLE
    assert lab_noworker.env.health["audit_sink_down"] is True


def test_readyz_fails_when_audit_sink_is_readable_but_not_writable(
        lab_noworker, monkeypatch):
    class ReadableButUnwritable:
        in_transaction = False

        def execute(self, sql, _args=()):
            if sql.startswith("PRAGMA busy_timeout"):
                return self
            raise sqlite3.OperationalError("readonly")

        def close(self):
            pass

    monkeypatch.setattr(server, "connect", lambda _path: ReadableButUnwritable())
    status, body = lab_noworker.raw(
        b"", method="GET", url=lab_noworker.url.replace("/mcp/", "/readyz"))
    assert status == 503 and body == {"status": "down"}
    assert lab_noworker.env.health["audit_sink_down"] is True


def _raw_post(lab, content_length: str, body: bytes, *, half_close: bool) -> bytes:
    sock = socket.create_connection(("127.0.0.1", lab.handle.port), timeout=2)
    try:
        request = (
            "POST /mcp/ HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"Authorization: Bearer {CLIENTS['mac-claude']}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {content_length}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request + body)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        sock.settimeout(2)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        sock.close()


@pytest.mark.parametrize("content_length", ["not-a-number", "9" * 4301])
def test_malformed_content_length_is_safe_and_audited(lab_noworker, content_length):
    response = _raw_post(lab_noworker, content_length, b"", half_close=False)
    assert response.startswith(b"HTTP/1.1 400")
    row = lab_noworker.audit_rows()[-1]
    assert row["state"] == "rejected" and row["outcome_code"] == "envelope_rejected"


def test_short_request_body_is_safe_and_audited(lab_noworker):
    response = _raw_post(lab_noworker, "10", b"{}", half_close=True)
    assert response.startswith(b"HTTP/1.1 400")
    row = lab_noworker.audit_rows()[-1]
    assert row["state"] == "rejected" and row["outcome_code"] == "body_read_incomplete"


def test_request_body_timeout_is_safe_and_audited(lab_noworker, monkeypatch):
    monkeypatch.setattr(C, "REQUEST_BODY_TIMEOUT_S", 0.1)
    response = _raw_post(lab_noworker, "10", b"{", half_close=False)
    assert response.startswith(b"HTTP/1.1 408")
    row = lab_noworker.audit_rows()[-1]
    assert row["state"] == "rejected" and row["outcome_code"] == "body_read_timeout"


def test_request_body_reader_restores_socket_timeout(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.timeout = 17.0
            self.changes = []

        def gettimeout(self):
            return self.timeout

        def settimeout(self, value):
            self.timeout = value
            self.changes.append(value)

    class FakeReader:
        @staticmethod
        def read1(length):
            return b"x" * length

    monkeypatch.setattr(C, "REQUEST_BODY_TIMEOUT_S", 0.25)
    monkeypatch.setattr(server.time, "monotonic", lambda: 10.0)
    handler = object.__new__(server.make_handler(object()))
    handler.connection = FakeConnection()
    handler.rfile = FakeReader()
    handler.close_connection = False
    assert handler._read_request_body(3) == b"xxx"
    assert handler.connection.changes == [0.25, 17.0]


def test_request_body_reader_enforces_absolute_deadline_against_slow_drip(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.timeout = 17.0
            self.changes = []

        def gettimeout(self):
            return self.timeout

        def settimeout(self, value):
            self.timeout = value
            self.changes.append(value)

    class DripReader:
        def __init__(self):
            self.reads = 0

        def read1(self, _length):
            self.reads += 1
            return b"x"

    clock = iter((100.0, 100.4, 100.8, 101.01))
    monkeypatch.setattr(C, "REQUEST_BODY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    handler = object.__new__(server.make_handler(object()))
    handler.connection = FakeConnection()
    handler.rfile = DripReader()
    handler.close_connection = False

    with pytest.raises(TimeoutError, match="deadline"):
        handler._read_request_body(3)

    assert handler.rfile.reads == 2
    assert handler.connection.timeout == 17.0

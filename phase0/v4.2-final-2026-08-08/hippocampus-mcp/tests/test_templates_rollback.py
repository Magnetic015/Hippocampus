"""P0.8/P0.12: template invariants and the two-manifest rollback contract.

Docker is not available in the Phase 0 workspace, so the container-level drill
stays open (see reports/phase0-report.md). What is verified here is everything
that does not need a daemon: manifest wiring, secret mapping, label separation,
and the script's refusal logic exercised through a stub `docker`.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
SCRIPTS = TEMPLATES / "scripts"
COMPOSE = (TEMPLATES / "compose.yaml").read_text(encoding="utf-8")


def test_only_mcp_publishes_a_host_port():
    ports = re.findall(r'^\s*-\s*"([^"]+:\d+:\d+)"', COMPOSE, re.M)
    assert ports == ["192.168.2.41:8888:8080"]
    assert "5432:" not in COMPOSE and "9999" not in COMPOSE


def test_every_secret_is_explicitly_mapped_and_required():
    for var in ("HIPPOCAMPUS_DB_PASSWORD", "HINDSIGHT_INTERNAL_TOKEN", "CLIPROXY_KEY",
                "HIPPOCAMPUS_TOKEN_PEPPER", "HIPPOCAMPUS_AUDIT_HMAC_KEY",
                "HIPPOCAMPUS_IDEMPOTENCY_HMAC_KEY"):
        assert f"${{{var}:?required}}" in COMPOSE, var


def test_numbered_llm_member_has_its_own_key():
    assert "HINDSIGHT_API_LLM_1_API_KEY: ${CLIPROXY_KEY:?required}" in COMPOSE


def test_hindsight_env_file_is_mounted_and_secret_free():
    assert "- ./hindsight.env" in COMPOSE
    env = (TEMPLATES / "hindsight.env").read_text(encoding="utf-8")
    assert "HINDSIGHT_ENABLE_CP=false" in env
    assert "HINDSIGHT_API_OTEL_TRACES_ENABLED=false" in env
    assert "HINDSIGHT_API_LLM_TRACE_ENABLED=false" in env
    assert "HINDSIGHT_API_STORE_DOCUMENT_TEXT=false" in env
    assert "HINDSIGHT_API_AUDIT_LOG_ENABLED=false" in env
    assigned = {line.split("=", 1)[0] for line in env.splitlines()
                if line and not line.startswith("#") and "=" in line}
    for name in assigned:
        assert not re.search(r"(API_KEY|PASSWORD|_SECRET|_TOKEN)$", name), name
        assert not name.startswith("OTEL_") and "OTEL_EXPORTER" not in name, name
    import sys
    sys.path.insert(0, str(ROOT / "scanner"))
    import secretscanner
    assert not [ln for ln in env.splitlines() if secretscanner.scan_text(ln)]


def test_hardening_and_resource_limits_present():
    assert COMPOSE.count("no-new-privileges:true") == 3
    assert COMPOSE.count("cap_drop: [ALL]") == 3
    assert COMPOSE.count("read_only: true") == 2  # mcp + hindsight
    for limit in ("memory: 3g", "memory: 1g", "memory: 512m"):
        assert limit in COMPOSE, limit
    for forbidden in ("privileged", "network_mode: host", "pid: host", "ipc: host",
                      "/var/run/docker.sock"):
        assert forbidden not in COMPOSE, forbidden


def test_mcp_does_not_hard_depend_on_hindsight():
    mcp = COMPOSE.split("hippocampus-mcp:")[1]
    assert "depends_on" not in mcp.split("networks:")[0]


def test_images_are_digest_pinned():
    assert "@sha256:" in COMPOSE
    assert not re.search(r"image:.*:latest", COMPOSE)


def test_env_example_carries_no_real_values():
    import sys
    sys.path.insert(0, str(ROOT / "scanner"))
    import secretscanner
    text = (TEMPLATES / "hippocampus.env.example").read_text(encoding="utf-8")
    findings = [line for line in text.splitlines() if secretscanner.scan_text(line)]
    assert not findings, findings


def test_prod_and_phase0_manifests_cannot_match_each_other():
    prod = (SCRIPTS / "rollback-manifest-prod.env").read_text(encoding="utf-8")
    assert "ROLLBACK_PROJECT=hippocampus\n" in prod
    assert "io.hippocampus.plan=v4.2\n" in prod
    assert "v4.2-phase0" not in prod  # disposable objects can never match production


def test_rollback_script_never_uses_down_v():
    text = (SCRIPTS / "rollback-server.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "down -v" not in code and "--volumes" not in code
    assert "refusing: command contains -v" in code  # runtime guard, not just intent
    assert not re.search(r"^ROLLBACK_PROJECT=", code, re.M)  # no hardcoded project


def _stub_docker(tmp_path: Path, *, project: str, labelled: bool) -> Path:
    """Fake `docker` reporting one project whose containers may lack the labels."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(f"""#!/usr/bin/env bash
args="$*"
if [[ "$args" == *"ps -aq"* ]]; then
  if [[ "$args" != *"com.docker.compose.project={project}"* ]]; then exit 0; fi
  if [[ "$args" == *"io.hippocampus"* && "{labelled}" != "True" ]]; then exit 0; fi
  echo c1; echo c2; exit 0
fi
if [[ "$args" == *"volume ls"* ]]; then echo hippocampus-pgdata; exit 0; fi
echo "docker $args" ; exit 0
""", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run(manifest: Path, mode: str, bin_dir: Path):
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(
        ["bash", str(SCRIPTS / "rollback-server.sh"), "--manifest", str(manifest), mode],
        capture_output=True, text=True, env=env)


@pytest.fixture
def phase0_manifest(tmp_path):
    m = tmp_path / "rollback-manifest-phase0.env"
    m.write_text(
        "ROLLBACK_PROJECT=hippocampus-v4-2-phase0-run1\n"
        "ROLLBACK_LABEL_MANAGED=io.hippocampus.managed=true\n"
        "ROLLBACK_LABEL_PLAN=io.hippocampus.plan=v4.2-phase0\n"
        f"ROLLBACK_COMPOSE_FILE={tmp_path}/compose.yaml\n", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    return m


def test_production_manifest_refused_against_disposable_project(tmp_path, phase0_manifest):
    """P0.12 first half: the production manifest must refuse the Phase 0 project."""
    bin_dir = _stub_docker(tmp_path, project="hippocampus-v4-2-phase0-run1", labelled=True)
    result = _run(SCRIPTS / "rollback-manifest-prod.env", "--dry-run", bin_dir)
    assert result.returncode == 1
    assert "REFUSED" in result.stderr


def test_disposable_manifest_dry_run_and_execute(tmp_path, phase0_manifest):
    """P0.12 second half: the run-scoped manifest works, without -v."""
    bin_dir = _stub_docker(tmp_path, project="hippocampus-v4-2-phase0-run1", labelled=True)
    dry = _run(phase0_manifest, "--dry-run", bin_dir)
    assert dry.returncode == 0 and "DRY-RUN" in dry.stdout
    assert " -v" not in dry.stdout

    ex = _run(phase0_manifest, "--execute", bin_dir)
    assert ex.returncode == 0
    assert "down --remove-orphans" in ex.stdout
    assert "volumes retained" in ex.stdout


def test_unlabelled_containers_are_refused(tmp_path, phase0_manifest):
    bin_dir = _stub_docker(tmp_path, project="hippocampus-v4-2-phase0-run1", labelled=False)
    result = _run(phase0_manifest, "--execute", bin_dir)
    assert result.returncode == 1 and "REFUSED" in result.stderr


def test_dockerfile_strips_failpoints_from_production_stage():
    text = (ROOT / "hippocampus-mcp" / "Dockerfile").read_text(encoding="utf-8")
    prod = text.split("FROM base AS production")[1]
    assert "rm -f src/hippocampus/failpoints.py" in prod
    assert "USER 1000:1000" in prod

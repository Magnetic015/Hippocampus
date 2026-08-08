"""MD Vault path mapping and durable write chain (plan 03 §3.2, 05 §5.2 R5)."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DOC_RE = re.compile(r"^mem_\d{8}_[0-9A-HJKMNP-TV-Z]{10,26}$")
URI_RE = re.compile(r"^memory://shared/([a-z0-9][a-z0-9_-]{0,63})/(mem_\d{8}_[0-9A-HJKMNP-TV-Z]{10,26})$")


class VaultError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def uri_for(project: str, document_id: str) -> str:
    return f"memory://shared/{project}/{document_id}"


def parse_uri(uri: str) -> tuple[str, str]:
    m = URI_RE.match(uri)
    if not m:
        raise VaultError("BAD_URI")
    return m.group(1), m.group(2)


def project_dir(root: str, project: str) -> Path:
    if not PROJECT_RE.match(project):
        raise VaultError("BAD_PROJECT")
    return Path(root) / "shared" / project


def doc_path(root: str, project: str, document_id: str) -> Path:
    if not DOC_RE.match(document_id):
        raise VaultError("BAD_DOCUMENT_ID")
    p = (project_dir(root, project) / f"{document_id}.md")
    resolved_root = Path(root).resolve()
    if resolved_root not in p.resolve().parents:
        raise VaultError("PATH_ESCAPE")
    return p


def staging_path(root: str, project: str, event_id: str) -> Path:
    return project_dir(root, project) / f".hippocampus-{event_id}.staging"


def refuse_symlink(path: Path) -> None:
    for part in (path, *path.parents):
        if part.exists() and part.is_symlink():
            raise VaultError("SYMLINK_REFUSED")


def _fsync_dir(dirpath: Path) -> None:
    fd = os.open(str(dirpath), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_staging(staging: Path, data: bytes) -> None:
    staging.parent.mkdir(parents=True, exist_ok=True)
    refuse_symlink(staging.parent)
    fd = os.open(str(staging), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(staging.parent)


def promote_staging(staging: Path, final: Path) -> None:
    os.rename(str(staging), str(final))
    _fsync_dir(final.parent)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())

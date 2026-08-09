"""MD Vault path mapping and durable write chain (plan 03 §3.2, 05 §5.2 R5)."""

from __future__ import annotations

import hashlib
import os
import re
import stat
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
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("write made no progress")
            written += count
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


def read_artifact(path: Path, *, max_bytes: int) -> bytes:
    """Read one strict 0600 regular artifact without following symlinks."""
    try:
        refuse_symlink(path)
        before = path.lstat()
    except FileNotFoundError:
        raise VaultError("ARTIFACT_MISSING") from None
    except VaultError:
        raise
    except OSError:
        raise VaultError("ARTIFACT_UNSAFE") from None
    if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
        raise VaultError("ARTIFACT_UNSAFE")

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif path.is_symlink():
        raise VaultError("ARTIFACT_UNSAFE")
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        raise VaultError("ARTIFACT_MISSING") from None
    except OSError:
        raise VaultError("ARTIFACT_UNSAFE") from None
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size > max_bytes):
            raise VaultError("ARTIFACT_UNSAFE")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(128 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise VaultError("ARTIFACT_UNSAFE")
        after = path.lstat()
        if ((after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(after.st_mode)
                or stat.S_IMODE(after.st_mode) != 0o600):
            raise VaultError("ARTIFACT_UNSAFE")
        return b"".join(chunks)
    except VaultError:
        raise
    except FileNotFoundError:
        raise VaultError("ARTIFACT_MISSING") from None
    except OSError:
        raise VaultError("ARTIFACT_UNSAFE") from None
    finally:
        os.close(fd)


def artifact_matches(path: Path, expected_sha256: str) -> bool:
    """Match a recovery artifact without following symlinks or opening special files."""
    try:
        refuse_symlink(path)
        before = path.lstat()
    except (OSError, VaultError):
        return False
    if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
        return False

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif path.is_symlink():
        return False
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return False
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return False
        if not stat.S_ISREG(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o600:
            return False
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 128 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest() == expected_sha256
    except OSError:
        return False
    finally:
        os.close(fd)

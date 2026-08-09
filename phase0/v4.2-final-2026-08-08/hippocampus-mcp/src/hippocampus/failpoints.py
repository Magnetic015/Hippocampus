"""Test-build failpoint switch (plan 09 §10.2). Production images delete this
module; the loader in `server.py` falls back to a no-op stub. All hooks are
inert unless HIPPOCAMPUS_TEST_BUILD=1."""

from __future__ import annotations

import os

_armed: dict[str, BaseException] = {}


def enabled() -> bool:
    return os.environ.get("HIPPOCAMPUS_TEST_BUILD") == "1"


def arm(name: str, exc: BaseException | None = None) -> None:
    _armed[name] = exc or RuntimeError(f"failpoint:{name}")


def clear(name: str | None = None) -> None:
    if name is None:
        _armed.clear()
    else:
        _armed.pop(name, None)


def hit(name: str) -> None:
    if enabled() and name in _armed:
        raise _armed.pop(name)

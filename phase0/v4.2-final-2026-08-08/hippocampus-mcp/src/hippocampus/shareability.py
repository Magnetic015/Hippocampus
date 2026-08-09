"""Reject facts whose subject is the committing host.

Every document this gateway stores is written with `scope: shared` and is
readable by every client bound to the project, so a fact that says "本机默认
Python 是 3.8" is false for every other reader: it names no host, and the one
it describes is not recoverable from the card.  Such a claim is refused at
commit time rather than stored and later mis-served.

The markers below are deictic -- they point at the writer's own machine.  A
fact that identifies its host by name stays acceptable, because the reader can
tell whom it is about.
"""

import re

# Deictic references to the committing host. Deliberately a fixed vocabulary:
# it keeps the audit category bounded and the rejection explainable.
MARKERS = (
    "本机", "本地", "本电脑", "本主机", "本台机器",
    "这台机器", "这台电脑", "这台主机", "我这台", "我这边",
    "我的机器", "我的电脑", "我的笔记本",
    "this machine", "this computer", "this host", "this laptop", "this box",
    "my machine", "my computer", "my laptop",
)

# General-purpose compounds that merely start with a marker and carry no claim
# about the writer's host.
ALLOWED_COMPOUNDS = ("本地化", "本地时间", "本地变量")

CATEGORY = "host_local"

_PATTERN = re.compile(
    "|".join(re.escape(m).replace(r"\ ", r"\s+") for m in
             sorted(MARKERS, key=len, reverse=True)),
    re.IGNORECASE,
)


def markers_in(text: str) -> list[str]:
    """Deictic host references in `text`, lowercased, deduplicated, sorted."""
    hits = set()
    for match in _PATTERN.finditer(text):
        if any(text.startswith(c, match.start()) for c in ALLOWED_COMPOUNDS):
            continue
        hits.add(match.group(0).lower())
    return sorted(hits)


def scan_fields(fields: dict[str, str | None]) -> tuple[list[str], list[str]]:
    """Return (field paths carrying a marker, the markers found)."""
    paths, markers = [], set()
    for path, value in fields.items():
        if value is None:
            continue
        found = markers_in(value)
        if found:
            paths.append(path)
            markers.update(found)
    return paths, sorted(markers)

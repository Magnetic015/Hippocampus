"""Deterministic local secret scanner (plan v4.2, 04-security / F4 sp1).

Pure functions over immutable rule tables: same input always yields the same
result. No I/O, no clock, no randomness. Never stores or returns the matched
value itself — only rule categories.
"""

from __future__ import annotations

import base64
import binascii
import re
import urllib.parse

SCAN_POLICY_VERSION = "sp1"

# ASCII-only boundaries: \b does not fire between CJK text and an adjacent
# ASCII secret, so a token pasted straight after Chinese would evade \b rules.
_L = r"(?<![A-Za-z0-9])"
_R = r"(?![A-Za-z0-9])"

# category -> compiled patterns
_RULES: dict[str, list[re.Pattern]] = {
    "private_key": [
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        re.compile(r"PuTTY-User-Key-File-\d"),
    ],
    "token": [
        re.compile(_L + r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}" + _R),  # JWT
        re.compile(_L + r"gh[pousr]_[A-Za-z0-9]{20,}" + _R),
        re.compile(_L + r"github_pat_[A-Za-z0-9_]{20,}" + _R),
        re.compile(_L + r"xox[baprs]-[A-Za-z0-9-]{10,}" + _R),
        re.compile(_L + r"sk-[A-Za-z0-9_-]{20,}" + _R),
        re.compile(r"(?i)" + _L + r"bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
        re.compile(r"(?i)" + _L + r"authorization\s*:\s*\S{8,}"),
    ],
    "credential": [
        re.compile(_L + r"AKIA[0-9A-Z]{16}" + _R),
        re.compile(r"(?i)" + _L + r"(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|client[_-]?secret|signing[_-]?secret|webhook[_-]?secret|token)" + _R + r"\s*[:=]\s*['\"]?[^\s'\"<>$]{8,}"),
        re.compile(r"otpauth://(totp|hotp)/\S+"),
        re.compile(r"(?i)" + _L + r"(BEGIN OPENSSH|ssh-ed25519 PRIVATE)" + _R),
    ],
    "dsn": [
        re.compile(r"(?i)" + _L + r"[a-z][a-z0-9+]{1,30}://[^\s:/@'\"]{1,64}:[^\s@'\"]{4,}@[^\s'\"]+"),
    ],
    "cookie_header": [
        re.compile(r"(?i)" + _L + r"set-cookie\s*:\s*\S+"),
        re.compile(r"(?i)" + _L + r"cookie\s*:\s*\S*(session|sid|auth|token)\S*="),
    ],
}

_B64_CAND = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")


def _scan_plain(text: str) -> set[str]:
    hits: set[str] = set()
    for category, patterns in _RULES.items():
        for pat in patterns:
            if pat.search(text):
                hits.add(category)
                break
    return hits


def _decoded_variants(text: str) -> list[str]:
    variants: list[str] = []
    if "%" in text:
        try:
            unq = urllib.parse.unquote(text, errors="strict")
            if unq != text:
                variants.append(unq)
        except Exception:
            pass
    for cand in _B64_CAND.findall(text)[:16]:
        pad = cand + "=" * (-len(cand) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                raw = decoder(pad)
            except (binascii.Error, ValueError):
                continue
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if decoded.isprintable() or "\n" in decoded:
                variants.append(decoded)
            break
    return variants


def scan_text(text: str) -> set[str]:
    """Return the set of rule categories hit by *text* (empty set = clean)."""
    if not isinstance(text, str) or not text:
        return set()
    hits = _scan_plain(text)
    stripped = re.sub(r"\s+", "", text)
    if stripped != text:
        hits |= _scan_plain(stripped)
    for variant in _decoded_variants(text):
        if _scan_plain(variant) or _scan_plain(re.sub(r"\s+", "", variant)):
            hits.add("encoded_secret")
    return hits


def scan_fields(fields: dict[str, object]) -> list[tuple[str, set[str]]]:
    """Scan a flat field map; returns [(field_name, categories)] for hits only."""
    findings: list[tuple[str, set[str]]] = []
    for name, value in fields.items():
        texts: list[str] = []
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, (list, tuple)):
            texts.extend(v for v in value if isinstance(v, str))
        for t in texts:
            cats = scan_text(t)
            if cats:
                findings.append((name, cats))
                break
    return findings

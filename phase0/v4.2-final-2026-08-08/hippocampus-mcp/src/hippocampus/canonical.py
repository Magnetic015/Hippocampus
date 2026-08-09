"""cv1 payload canonicalization + HMAC (plan 05 §5.2, F4).

Covers only client-controlled, already-authorized business fields. Optional
fields use fixed absent representation (omitted key). Any change to these
rules requires a new canonical_version via plan revision.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata

from .constants import CANONICAL_VERSION

_CLIENT_FIELDS = ("title", "summary", "retrieval_text", "detail_body", "event_at", "project", "type")


def canonical_payload(fields: dict) -> bytes:
    doc: dict[str, str] = {"_cv": CANONICAL_VERSION}
    for key in _CLIENT_FIELDS:
        value = fields.get(key)
        if value is None:
            continue
        doc[key] = unicodedata.normalize("NFC", value)
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_hmac(key: bytes, fields: dict) -> str:
    return hmac.new(key, canonical_payload(fields), hashlib.sha256).hexdigest()

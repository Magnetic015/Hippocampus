"""Canary suite for the sp1 scanner (plan 09 §10.3). All canaries are synthetic."""

import base64

import secretscanner as ss

CANARIES = [
    ("pem", "-----BEGIN RSA PRIVATE KEY-----\nMIICanaryOnly\n-----END RSA PRIVATE KEY-----"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.c2lnbmF0dXJlLWNhbmFyeQ"),
    ("aws", "AKIACANARY0EXAMPLE99"),
    ("ghp", "ghp_Canary0123456789abcdefghij"),
    ("slack", "xoxb-123456789012-canaryToken"),
    ("openai", "sk-canary0123456789ABCDEFghijkl"),
    ("bearer", "Bearer canary-token-0123456789abcdef"),
    ("authz-header", "Authorization: Basic Y2FuYXJ5OnBhc3M="),
    ("password-kv", "password = SuperCanary123!"),
    ("apikey-kv", 'api_key: "canary-9f8e7d6c5b4a"'),
    ("dsn", "postgresql://svc:canaryPass123@db.example.internal:5432/main"),
    ("otpauth", "otpauth://totp/Example:user?secret=CANARYSECRET234567"),
    ("cookie", "Cookie: session_id=canary0123456789abcdef"),
    ("set-cookie", "Set-Cookie: auth=canary; HttpOnly"),
]

ALLOWED = [
    "${HIPPOCAMPUS_CLAUDE_TOKEN}",
    "<REDACTED>",
    "外部秘密管理器条目:op://vault/pi5-db 已于 2026-08-01 轮换",
    "指纹 SHA256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08(非认证用途)",
    "Pi5 192.168.2.41:8888 部署 Hippocampus MCP,memory_commit p95 500ms",
]


def test_all_canaries_hit():
    for name, canary in CANARIES:
        assert ss.scan_text(canary), f"canary not detected: {name}"


def test_encoded_and_split_variants_hit():
    raw = "password = SuperCanary123!"
    b64 = base64.b64encode(raw.encode()).decode()
    assert "encoded_secret" in ss.scan_text(f"备注:{b64}")
    url = "password%20%3D%20SuperCanary123%21"
    assert ss.scan_text(url)
    split = "AKIA CANARY0 EXAMPLE99"
    assert ss.scan_text(split)


def test_cjk_adjacent_secrets_still_detected():
    """\\b does not fire between CJK and ASCII: boundaries must be ASCII-only."""
    for canary in ("AKIACANARY0EXAMPLE99", "ghp_Canary0123456789abcdefghij",
                   "sk-canary0123456789ABCDEFghijkl"):
        assert ss.scan_text(f"备注{canary}结束"), canary
        assert ss.scan_text(f"密钥是{canary}"), canary


def test_allowed_content_passes():
    for text in ALLOWED:
        assert not ss.scan_text(text), f"false positive: {text!r}"


def test_deterministic():
    for _, canary in CANARIES:
        assert ss.scan_text(canary) == ss.scan_text(canary)


def test_scan_fields_reports_field_names_only():
    findings = ss.scan_fields({"title": "正常标题", "detail_body": "password = SuperCanary123!"})
    assert [f[0] for f in findings] == ["detail_body"]
    assert all(isinstance(c, set) for _, c in findings)


def test_version_pinned():
    assert ss.SCAN_POLICY_VERSION == "sp1"

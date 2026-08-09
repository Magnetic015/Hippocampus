"""Frozen contract constants for plan v4.2 (P0.1 contract freeze).

Every enum/limit here mirrors a section of plan/v4.2-final-2026-08-08 and must
not drift from it without a plan revision.
"""

VERSION = "v4.2-phase0"
CANONICAL_VERSION = "cv1"

BANK_MAIN = "main"
BANK_COMMISSIONING = "commissioning-v4-2"
MODE_PRODUCTION = "production"
MODE_COMMISSIONING = "commissioning"
COMMISSIONING_PROJECT = "commissioning"

# 07 §7.0 ingress hard limits
MAX_BODY_BYTES = 256 * 1024
REQUEST_BODY_TIMEOUT_S = 5.0
MAX_JSON_DEPTH = 8
MAX_OBJECT_FIELDS = 64
MAX_ARRAY_ITEMS = 64
MAX_ENVELOPE_STR_BYTES = 256

# 07 §7.3 field limits
MAX_TITLE_SCALARS = 256
SUMMARY_MIN_SCALARS = 60
SUMMARY_MAX_SCALARS = 200
RETRIEVAL_MIN_TOKENS = 100
RETRIEVAL_SOFT_MAX_TOKENS = 600
RETRIEVAL_HARD_MAX_TOKENS = 800
MAX_DETAIL_BYTES = 128 * 1024
MAX_MD_BYTES = 192 * 1024
# 07 §7.1 / §7.2
MAX_QUERY_SCALARS = 4096
MAX_READ_URIS = 2
MAX_READ_DOC_BYTES = 192 * 1024
MAX_READ_TOTAL_BYTES = 256 * 1024
SEARCH_MAX_CARDS = 5
RECALL_BUDGET = "low"
RECALL_MAX_TOKENS = 2048

TYPES = ("decision", "procedure", "fact", "incident", "preference", "constraint", "reference")

# 05 §5.1 state domains
RESERVATION_STATES = ("reserved", "retryable_failed", "prepared", "stored", "rejected", "conflict")
OUTBOX_STATES = ("prepared", "ready", "indexing", "retry_wait", "indexed", "dead", "policy_blocked", "conflict")
AUDIT_STATES = (
    "started", "prepared", "stored_pending", "ok", "idempotent_replay",
    "rejected", "error", "interrupted", "recovery_pending",
)
AUDIT_TERMINAL = frozenset(
    {"ok", "idempotent_replay", "rejected", "error", "interrupted", "recovery_pending"}
)

# 05 §5.1 lease contract (F4)
RESERVATION_LEASE_TTL_S = 60
OUTBOX_LEASE_TTL_S = 300
# 07 §7.6 audit retention.  Maintenance runs in ordered, bounded transactions
# so a long-lived gateway cannot grow this local metadata table without limit.
AUDIT_RETENTION_DAYS = 90
AUDIT_RETENTION_MAX_ROWS = 1_000_000
AUDIT_RETENTION_BATCH_SIZE = 10_000
AUDIT_RETENTION_MAX_BATCHES = 100
AUDIT_RETENTION_RUN_BUDGET_S = 5.0
AUDIT_RETENTION_INTERVAL_S = 60 * 60
AUDIT_RETENTION_RETRY_INTERVAL_S = 60
# 05 §5.4 worker retry
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 15 * 60.0
MAX_ATTEMPTS = 8

OPERATIONS = (
    "initialize", "initialized", "tools_list", "tools_call", "session_get", "session_delete",
)
TOOLS = ("memory_search", "memory_read", "memory_commit", "memory_status")
INVALID_TOOL = "invalid_tool"
UNKNOWN_FIELD = "unknown_field"

# audit-safe field path enums (04 §4.1/§4.2)
FIELD_PATHS = (
    "idempotency_key", "title", "summary", "retrieval_text", "detail_body", "event_at",
    "project", "type", "query", "filters", "uris", "document_id", "status_key",
    "jsonrpc_method", "jsonrpc_id", "jsonrpc_tool_name", UNKNOWN_FIELD,
)

# error codes (safe enums)
E_SECRET_REJECTED = "HIPPOCAMPUS_SECRET_REJECTED"
E_SCAN_UNAVAILABLE = "HIPPOCAMPUS_SECRET_SCAN_UNAVAILABLE"
E_INDEX_UNAVAILABLE = "HIPPOCAMPUS_INDEX_UNAVAILABLE"
E_AUDIT_UNAVAILABLE = "HIPPOCAMPUS_AUDIT_UNAVAILABLE"
E_IDEMPOTENCY_CONFLICT = "HIPPOCAMPUS_IDEMPOTENCY_CONFLICT"
E_RESERVED_FIELD = "RESERVED_FIELD_FORBIDDEN"
E_UNAVAILABLE = "HIPPOCAMPUS_UNAVAILABLE"
E_ENVELOPE_REJECTED = "HIPPOCAMPUS_ENVELOPE_REJECTED"
E_SCHEMA_REJECTED = "HIPPOCAMPUS_SCHEMA_REJECTED"
E_AUTHZ_DENIED = "HIPPOCAMPUS_AUTHORIZATION_DENIED"
E_LIMIT_EXCEEDED = "HIPPOCAMPUS_LIMIT_EXCEEDED"
E_NOT_FOUND = "HIPPOCAMPUS_NOT_FOUND"
E_DURABILITY_GAP = "DURABILITY_GAP"
E_STATE_INVARIANT = "STATE_INVARIANT_BROKEN"
E_HASH_MISMATCH = "HASH_MISMATCH"
E_HINDSIGHT_TIMEOUT = "HINDSIGHT_TIMEOUT"
E_UPSTREAM_5XX = "UPSTREAM_5XX"
E_POLICY_BLOCKED = "POLICY_BLOCKED"

OUTCOME_COMMIT_OK = "COMMIT_STORED_INDEX_PENDING"
OUTCOME_SOURCE_DENIED = "source_denied"
OUTCOME_AUTH_FAILED = "auth_failed"
OUTCOME_SOURCE_MISMATCH = "source_mismatch"
OUTCOME_SECRET_REJECTED = "secret_rejected"
OUTCOME_FINALIZE_LOST = "AUDIT_FINALIZE_LOST"

RESERVED_COMMIT_FIELDS = frozenset(
    {"bank", "server_bank_id", "uri", "document_id", "tags", "trust", "scope",
     "sensitivity", "src", "source_agent", "request_id", "root_request_id", "event_id"}
)


def index_state_projection(outbox_status: str | None, reservation_state: str | None) -> str:
    """External index_state per 05 §5.5. Outbox is authoritative when present."""
    if outbox_status is not None:
        if outbox_status in ("ready", "retry_wait"):
            return "index_pending"
        if outbox_status == "prepared":
            return "recovery_pending"
        return outbox_status  # indexing/indexed/dead/policy_blocked/conflict
    if reservation_state in ("reserved", "retryable_failed"):
        return "recovery_pending"
    if reservation_state in ("rejected", "conflict"):
        return reservation_state
    return "conflict"  # stored without outbox and anything else = invariant broken

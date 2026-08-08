"""The four MCP data tools (plan 07), sharing one auth/schema/scan/audit gate."""

from __future__ import annotations

import datetime as _dt
import re
import uuid

from . import audit, canonical, constants as C, failpoints, statemachine as sm, textlimits, vault
from .audit import AuditError
from .hindsight_client import HindsightError
from .mdrender import render

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class ToolError(Exception):
    def __init__(self, code: str, *, state: str = "rejected", outcome: str | None = None,
                 retryable: bool = False, **extra):
        super().__init__(code)
        self.code = code
        self.state = state
        self.outcome = outcome or code
        self.retryable = retryable
        self.extra = extra

    def payload(self) -> dict:
        p = {"code": self.code, "retryable": self.retryable}
        p.update(self.extra)
        return p


def _new_document_id() -> str:
    raw = uuid.uuid4().bytes + uuid.uuid4().bytes[:1]
    num = int.from_bytes(raw[:17], "big")
    chars = []
    for _ in range(26):
        num, rem = divmod(num, 32)
        chars.append(_CROCKFORD[rem])
    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
    return f"mem_{day}_{''.join(chars)}"


def _rfc3339_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def normalize_event_at(value: str | None) -> str:
    if value is None or value == "unset":
        return "unset"
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["event_at"]) from None
    if dt.tzinfo is None:
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["event_at"])
    return dt.isoformat(timespec="seconds")


def ensure_scanner(env) -> None:
    if env.scanner_down:
        raise ToolError(C.E_SCAN_UNAVAILABLE, outcome="scanner_unavailable", retryable=True)


def scan_or_reject(env, fields: dict[str, str | None]) -> None:
    ensure_scanner(env)
    findings = env.scan_fields({k: v for k, v in fields.items() if v is not None})
    if findings:
        paths = [f if f in C.FIELD_PATHS else C.UNKNOWN_FIELD for f, _ in findings]
        cats = sorted({c for _, cs in findings for c in cs})
        raise ToolError(C.E_SECRET_REJECTED, outcome=C.OUTCOME_SECRET_REJECTED,
                        stored=False, indexed=False, fields=paths, categories=cats)


def _strict_schema(args: dict, required: dict[str, type], optional: dict[str, type],
                   *, check_reserved: bool = False) -> None:
    if check_reserved:
        reserved = C.RESERVED_COMMIT_FIELDS & set(args)
        if reserved:
            raise ToolError(C.E_RESERVED_FIELD, fields=sorted(
                f if f in C.FIELD_PATHS else C.UNKNOWN_FIELD for f in reserved))
    unknown = set(args) - set(required) - set(optional)
    if unknown:
        raise ToolError(C.E_SCHEMA_REJECTED, fields=[C.UNKNOWN_FIELD])
    for name, typ in required.items():
        if name not in args or not isinstance(args[name], typ):
            raise ToolError(C.E_SCHEMA_REJECTED, fields=[name if name in C.FIELD_PATHS else C.UNKNOWN_FIELD])
    for name, typ in optional.items():
        if name in args and not isinstance(args[name], typ):
            raise ToolError(C.E_SCHEMA_REJECTED, fields=[name if name in C.FIELD_PATHS else C.UNKNOWN_FIELD])


def _authorize(env, ctx, *, action: str, project: str, type_: str | None = None) -> None:
    from .registry import AuthzError, authorize
    try:
        authorize(ctx.client_row, action=action, project=project, type_=type_)
    except AuthzError:
        raise ToolError(C.E_AUTHZ_DENIED) from None


# ---------------------------------------------------------------- memory_commit

def _commit_response(res, out) -> dict:
    state = C.index_state_projection(out["status"] if out else None, res["state"])
    return {
        "accepted": True,
        "stored": res["state"] == "stored",
        "indexed": out is not None and out["status"] == "indexed",
        "index_state": state,
        "document_id": res["document_id"],
        "uri": res["uri"],
    }


def memory_commit(env, ctx, args: dict) -> dict:
    _strict_schema(
        args,
        {"idempotency_key": str, "title": str, "summary": str, "retrieval_text": str,
         "project": str, "type": str},
        {"detail_body": str, "event_at": str},
        check_reserved=True,
    )
    if not UUID4_RE.match(args["idempotency_key"]):
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["idempotency_key"])
    if not vault.PROJECT_RE.match(args["project"]):
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["project"])
    if args["type"] not in C.TYPES:
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["type"])
    if textlimits.scalar_len(args["title"]) > C.MAX_TITLE_SCALARS:
        raise ToolError(C.E_LIMIT_EXCEEDED, fields=["title"])
    s_len = textlimits.scalar_len(args["summary"])
    if not (C.SUMMARY_MIN_SCALARS <= s_len <= C.SUMMARY_MAX_SCALARS):
        raise ToolError(C.E_LIMIT_EXCEEDED, fields=["summary"])
    tokens = env.token_count(args["retrieval_text"])
    if tokens < C.RETRIEVAL_MIN_TOKENS or tokens > C.RETRIEVAL_HARD_MAX_TOKENS:
        raise ToolError(C.E_LIMIT_EXCEEDED, fields=["retrieval_text"])
    detail = args.get("detail_body")
    if detail is not None and len(detail.encode("utf-8")) > C.MAX_DETAIL_BYTES:
        raise ToolError(C.E_LIMIT_EXCEEDED, fields=["detail_body"])
    event_at = normalize_event_at(args.get("event_at"))

    project = args["project"]
    if env.mode == C.MODE_COMMISSIONING and project != C.COMMISSIONING_PROJECT:
        raise ToolError(C.E_AUTHZ_DENIED, fields=["project"])
    _authorize(env, ctx, action="write", project=project, type_=args["type"])
    scan_or_reject(env, {
        "idempotency_key": args["idempotency_key"], "title": args["title"],
        "summary": args["summary"], "retrieval_text": args["retrieval_text"],
        "detail_body": detail, "event_at": event_at, "project": project, "type": args["type"],
    })

    cfields = {"title": args["title"], "summary": args["summary"],
               "retrieval_text": args["retrieval_text"], "detail_body": detail,
               "event_at": args.get("event_at"), "project": project, "type": args["type"]}
    phmac = canonical.payload_hmac(env.idem_key, cfields)
    conn = ctx.conn

    failpoints.hit("commit.reserve.before")
    sm.begin_immediate(conn)
    try:
        res = sm.lookup_reservation(conn, ctx.client_id, args["idempotency_key"])
        if res is not None:
            if res["canonical_version"] != C.CANONICAL_VERSION or res["payload_hmac"] != phmac:
                conn.execute("COMMIT")
                audit.finalize(conn, ctx.request_id, "rejected", "idempotency_conflict")
                ctx.audit_done = True
                raise ToolError(C.E_IDEMPOTENCY_CONFLICT, http_status=409)
            audit.link_event(conn, ctx.request_id, res["root_request_id"], res["event_id"])
            resumable = (res["state"] in ("reserved", "retryable_failed")
                         and sm.take_reservation_lease(conn, res["event_id"], env.pid)
                         and env.try_lock_event(res["event_id"]))
            conn.execute("COMMIT")
            if not resumable:
                out = conn.execute("SELECT * FROM outbox WHERE event_id=?",
                                   (res["event_id"],)).fetchone()
                audit.finalize(conn, ctx.request_id, "idempotent_replay", "idempotent_replay")
                ctx.audit_done = True
                if res["state"] == "rejected":
                    raise ToolError(C.E_SECRET_REJECTED, outcome=C.OUTCOME_SECRET_REJECTED)
                return _commit_response(res, out)
        else:
            document_id = _new_document_id()
            event_id = uuid.uuid4().hex
            sm.insert_reserved(
                conn, client_id=ctx.client_id, key=args["idempotency_key"], phmac=phmac,
                cv=C.CANONICAL_VERSION, event_id=event_id, root_request_id=ctx.request_id,
                bank=env.bank, document_id=document_id, uri=vault.uri_for(project, document_id),
                doc_created_at=_rfc3339_now(), owner=env.pid)
            audit.link_event(conn, ctx.request_id, ctx.request_id, event_id)
            env.try_lock_event(event_id)  # brand-new event: always ours
            conn.execute("COMMIT")
            res = sm.lookup_reservation(conn, ctx.client_id, args["idempotency_key"])
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    try:
        return _commit_files(env, ctx, res, args, detail, event_at, project)
    finally:
        env.unlock_event(res["event_id"])


def _commit_files(env, ctx, res, args: dict, detail: str | None, event_at: str, project: str) -> dict:
    conn = ctx.conn
    md = render(
        {"id": res["document_id"], "title": args["title"], "summary": args["summary"],
         "uri": res["uri"], "project": project, "source_agent": ctx.source_tag,
         "scope": "shared", "trust": "agent", "sensitivity": "internal", "type": args["type"],
         "created_at": res["document_created_at"], "updated_at": res["document_created_at"],
         "event_at": event_at,
         "tags": [f"src:{ctx.source_tag}", f"project:{project}", "scope:shared",
                  "trust:agent", "sensitivity:internal", f"type:{args['type']}"]},
        args["retrieval_text"], detail)
    if len(md) > C.MAX_MD_BYTES:
        sm.set_reservation_state(conn, res["event_id"], "rejected", release_lease=True)
        audit.finalize(conn, ctx.request_id, "rejected", "limit_exceeded")
        ctx.audit_done = True
        raise ToolError(C.E_LIMIT_EXCEEDED, fields=["detail_body"])
    ensure_scanner(env)
    if env.scan_text(md.decode("utf-8")):
        sm.set_reservation_state(conn, res["event_id"], "rejected", release_lease=True)
        audit.finalize(conn, ctx.request_id, "rejected", C.OUTCOME_SECRET_REJECTED)
        ctx.audit_done = True
        raise ToolError(C.E_SECRET_REJECTED, outcome=C.OUTCOME_SECRET_REJECTED)
    desired_sha = vault.sha256_bytes(md)

    staging = vault.staging_path(env.vault_root, project, res["event_id"])
    final = vault.doc_path(env.vault_root, project, res["document_id"])
    existing_out = ctx.conn.execute("SELECT * FROM outbox WHERE event_id=?",
                                    (res["event_id"],)).fetchone()
    if existing_out is None:
        try:
            failpoints.hit("commit.staging.before")
            if not staging.exists():
                vault.write_staging(staging, md)
            failpoints.hit("commit.staging.after")
        except ToolError:
            raise
        except BaseException:
            staging.unlink(missing_ok=True)
            sm.set_reservation_state(conn, res["event_id"], "retryable_failed", release_lease=True)
            _finalize_quiet(conn, ctx, "error", "retryable_failed")
            raise ToolError(C.E_UNAVAILABLE, state="error", outcome="retryable_failed",
                            retryable=True) from None
        try:
            failpoints.hit("commit.prepared.before")
            sm.begin_immediate(conn)
            sm.insert_outbox_prepared(conn, res, desired_sha, env.spv)
            sm.set_reservation_state(conn, res["event_id"], "prepared")
            audit.set_state(conn, ctx.request_id, "prepared")
            conn.execute("COMMIT")
            failpoints.hit("commit.prepared.after")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            staging.unlink(missing_ok=True)
            sm.set_reservation_state(conn, res["event_id"], "retryable_failed", release_lease=True)
            _finalize_quiet(conn, ctx, "error", "retryable_failed")
            raise ToolError(C.E_UNAVAILABLE, state="error", outcome="retryable_failed",
                            retryable=True) from None
    try:
        failpoints.hit("commit.rename.before")
        if staging.exists():
            vault.promote_staging(staging, final)
        failpoints.hit("commit.rename.after")
    except BaseException:
        _finalize_quiet(conn, ctx, "recovery_pending", "rename_failed")
        return {"accepted": True, "stored": False, "indexed": False,
                "index_state": "recovery_pending", "document_id": res["document_id"],
                "uri": res["uri"]}
    try:
        sm.begin_immediate(conn)
        sm.set_outbox_status(conn, res["event_id"], "ready")
        sm.set_reservation_state(conn, res["event_id"], "stored", release_lease=True)
        audit.set_state(conn, ctx.request_id, "stored_pending")
        failpoints.hit("commit.ready.txn")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        _finalize_quiet(conn, ctx, "recovery_pending", "ready_txn_failed")
        return {"accepted": True, "stored": True, "indexed": False,
                "index_state": "recovery_pending", "document_id": res["document_id"],
                "uri": res["uri"]}
    try:
        audit.finalize(conn, ctx.request_id, "ok", C.OUTCOME_COMMIT_OK)
        ctx.audit_done = True
    except AuditError:
        ctx.audit_done = True
        return {"accepted": True, "stored": True, "indexed": False,
                "index_state": "recovery_pending", "document_id": res["document_id"],
                "uri": res["uri"]}
    return {"accepted": True, "stored": True, "indexed": False, "index_state": "index_pending",
            "document_id": res["document_id"], "uri": res["uri"]}


def _finalize_quiet(conn, ctx, state: str, outcome: str) -> None:
    try:
        audit.finalize(conn, ctx.request_id, state, outcome)
        ctx.audit_done = True
    except AuditError:
        ctx.audit_done = True


# ---------------------------------------------------------------- memory_search

def memory_search(env, ctx, args: dict) -> dict:
    _strict_schema(args, {"query": str, "project": str}, {"type": str, "source": str})
    if textlimits.scalar_len(args["query"]) > C.MAX_QUERY_SCALARS:
        raise ToolError(C.E_LIMIT_EXCEEDED, fields=["query"])
    if not vault.PROJECT_RE.match(args["project"]):
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["project"])
    ensure_scanner(env)
    if env.scan_text(args["query"]):
        raise ToolError(C.E_SECRET_REJECTED, outcome=C.OUTCOME_SECRET_REJECTED, fields=["query"])
    ctx.query_hmac, ctx.query_len = audit.safe_query_hmac(env.audit_key, args["query"])
    if "type" in args and args["type"] not in C.TYPES:
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["type"])
    source = args.get("source")
    if source is not None and source not in env.known_source_tags():
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["filters"])
    _authorize(env, ctx, action="read", project=args["project"])

    tag_groups = [
        {"tags": [f"project:{args['project']}"], "match": "all_strict"},
        {"tags": ["scope:shared"], "match": "all_strict"},
    ]
    if "type" in args:
        tag_groups.append({"tags": [f"type:{args['type']}"], "match": "all_strict"})
    if source is not None:
        tag_groups.append({"tags": [f"src:{source}"], "match": "all_strict"})
    try:
        raw = env.hindsight.recall(env.bank, args["query"], tag_groups,
                                   budget=C.RECALL_BUDGET, max_tokens=C.RECALL_MAX_TOKENS)
    except HindsightError:
        raise ToolError(C.E_INDEX_UNAVAILABLE, state="error", outcome="index_unavailable",
                        retryable=True) from None

    groups: dict[str, dict] = {}
    for fact in raw.get("results", []):
        doc = fact.get("document_id")
        if not isinstance(doc, str):
            continue
        group = groups.setdefault(doc, {"facts": [], "bad": False})
        group["facts"].append(fact)
        meta = fact.get("metadata") or {}
        tags = fact.get("tags") or []
        project_tags = [t for t in tags if isinstance(t, str) and t.startswith("project:")]
        try:
            uproj, udoc = vault.parse_uri(meta.get("uri", ""))
        except vault.VaultError:
            group["bad"] = True
            continue
        if (project_tags != [f"project:{args['project']}"] or "scope:shared" not in tags
                or uproj != args["project"] or udoc != doc
                or not isinstance(meta.get("event_at"), str)):
            group["bad"] = True
    cards = []
    for doc, group in groups.items():
        if group["bad"]:
            continue
        metas = {tuple(sorted((f.get("metadata") or {}).items())) for f in group["facts"]}
        tagsets = {tuple(sorted(f.get("tags") or [])) for f in group["facts"]}
        uris = {(f.get("metadata") or {}).get("uri") for f in group["facts"]}
        if len(metas) > 1 or len(tagsets) > 1 or len(uris) > 1:
            continue
        best = max(group["facts"], key=lambda f: f.get("score", 0.0))
        meta = best.get("metadata") or {}
        card_texts = [meta.get("index_title", ""), meta.get("index_summary", ""),
                      best.get("content", "")]
        if any(env.scan_text(t) for t in card_texts if t):
            continue
        res_row, out_row = sm.status_by_ref(ctx.conn, document_id=doc)
        index_state = (C.index_state_projection(out_row["status"] if out_row else None,
                                                res_row["state"]) if res_row else "conflict")
        cards.append({
            "score": best.get("score", 0.0),
            "card": {
                "document_id": doc, "uri": meta.get("uri"),
                "index_title": meta.get("index_title"), "index_summary": meta.get("index_summary"),
                "safe_snippet": best.get("content", "")[:512],
                "tags": sorted(best.get("tags") or []), "source_agent": meta.get("source_agent"),
                "trust": meta.get("trust"), "event_at": meta.get("event_at"),
                "index_state": index_state,
            },
        })
    cards.sort(key=lambda c: c["score"], reverse=True)
    return {"results": [c["card"] for c in cards[:C.SEARCH_MAX_CARDS]]}


# ------------------------------------------------------------------ memory_read

def memory_read(env, ctx, args: dict) -> dict:
    _strict_schema(args, {"uris": list}, {})
    uris = args["uris"]
    if not (1 <= len(uris) <= C.MAX_READ_URIS) or not all(isinstance(u, str) for u in uris):
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["uris"])
    ensure_scanner(env)
    scan_or_reject(env, {"uris": uris})
    docs, total = [], 0
    for uri in uris:
        try:
            project, doc_id = vault.parse_uri(uri)
        except vault.VaultError:
            raise ToolError(C.E_SCHEMA_REJECTED, fields=["uris"]) from None
        _authorize(env, ctx, action="read", project=project)
        path = vault.doc_path(env.vault_root, project, doc_id)
        vault.refuse_symlink(path)
        res_row, out_row = sm.status_by_ref(ctx.conn, document_id=doc_id)
        if res_row is None:  # unowned orphans are never readable (05 §5.3)
            raise ToolError(C.E_NOT_FOUND)
        if out_row is not None and out_row["status"] in ("policy_blocked", "conflict"):
            raise ToolError(C.E_POLICY_BLOCKED if out_row["status"] == "policy_blocked"
                            else C.E_STATE_INVARIANT)
        if not path.exists():
            raise ToolError(C.E_NOT_FOUND)
        data = path.read_bytes()
        if len(data) > C.MAX_READ_DOC_BYTES:
            raise ToolError(C.E_LIMIT_EXCEEDED, fields=["uris"])
        if out_row is not None and vault.sha256_bytes(data) != out_row["desired_sha256"]:
            if env.scan_text(data.decode("utf-8", errors="replace")):
                sm.set_outbox_status(ctx.conn, out_row["event_id"], "policy_blocked",
                                     error_code=C.E_POLICY_BLOCKED)
                raise ToolError(C.E_POLICY_BLOCKED)
        total += len(data)
        if total > C.MAX_READ_TOTAL_BYTES:
            raise ToolError(C.E_LIMIT_EXCEEDED, fields=["uris"])
        docs.append({"uri": uri, "markdown": data.decode("utf-8", errors="replace")})
    return {"documents": docs}


# ---------------------------------------------------------------- memory_status

def memory_status(env, ctx, args: dict) -> dict:
    _strict_schema(args, {}, {"document_id": str, "idempotency_key": str})
    keys = [k for k in ("document_id", "idempotency_key") if k in args]
    if len(keys) != 1:
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["status_key"])
    ensure_scanner(env)
    if "document_id" in args:
        if not vault.DOC_RE.match(args["document_id"]):
            raise ToolError(C.E_SCHEMA_REJECTED, fields=["document_id"])
        res, out = sm.status_by_ref(ctx.conn, document_id=args["document_id"])
    else:
        if not UUID4_RE.match(args["idempotency_key"]):
            raise ToolError(C.E_SCHEMA_REJECTED, fields=["idempotency_key"])
        res, out = sm.status_by_ref(ctx.conn, client_id=ctx.client_id,
                                    idempotency_key=args["idempotency_key"])
    if res is None:
        raise ToolError(C.E_NOT_FOUND)
    project, _ = vault.parse_uri(res["uri"])
    _authorize(env, ctx, action="read", project=project)
    if res["state"] == "stored" and out is None:
        env.health["invariant_broken"] = True
        return {"document_id": res["document_id"], "index_state": "conflict",
                "error_code": C.E_STATE_INVARIANT}
    return {
        "document_id": res["document_id"],
        "index_state": C.index_state_projection(out["status"] if out else None, res["state"]),
        "attempt": out["attempt"] if out else 0,
        "next_attempt_at": out["next_attempt_at"] if out else None,
        "error_code": out["error_code"] if out else None,
    }


DISPATCH = {
    "memory_commit": memory_commit,
    "memory_search": memory_search,
    "memory_read": memory_read,
    "memory_status": memory_status,
}

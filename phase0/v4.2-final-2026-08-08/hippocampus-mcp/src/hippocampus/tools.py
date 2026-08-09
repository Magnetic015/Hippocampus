"""The four MCP data tools (plan 07), sharing one auth/schema/scan/audit gate."""

from __future__ import annotations

import datetime as _dt
import math
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


def _business_value_contains_request_token(ctx, value) -> bool:
    if isinstance(value, str):
        return ctx.business_value_contains_token(value)
    if isinstance(value, (list, tuple)):
        return any(_business_value_contains_request_token(ctx, item) for item in value)
    if isinstance(value, dict):
        return any(
            _business_value_contains_request_token(ctx, key)
            or _business_value_contains_request_token(ctx, item)
            for key, item in value.items()
        )
    return False


def _raise_request_token(fields: set[str]) -> None:
    raise ToolError(
        C.E_SECRET_REJECTED,
        outcome=C.OUTCOME_SECRET_REJECTED,
        stored=False,
        indexed=False,
        fields=sorted(fields),
        categories=["token"],
    )


def reject_envelope_request_token(ctx, envelope: dict) -> None:
    params = envelope.get("params")
    tool_name = params.get("name") if isinstance(params, dict) else None
    fields = {
        field for field, value in (
            ("jsonrpc_id", envelope.get("id")),
            ("jsonrpc_method", envelope.get("method")),
            ("jsonrpc_tool_name", tool_name),
        )
        if _business_value_contains_request_token(ctx, value)
    }
    if fields:
        _raise_request_token(fields)


def reject_request_token(ctx, args: dict) -> None:
    fields: set[str] = set()
    for name, value in args.items():
        if not _business_value_contains_request_token(ctx, value):
            continue
        field = "filters" if name == "source" else name
        fields.add(field if field in C.FIELD_PATHS else C.UNKNOWN_FIELD)
    if fields:
        _raise_request_token(fields)


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


def _render_commit_document(ctx, res, args: dict, detail: str | None,
                            event_at: str, project: str) -> bytes:
    return render(
        {"id": res["document_id"], "title": args["title"], "summary": args["summary"],
         "uri": res["uri"], "project": project, "source_agent": ctx.source_tag,
         "scope": "shared", "trust": "agent", "sensitivity": "internal", "type": args["type"],
         "created_at": res["document_created_at"], "updated_at": res["document_created_at"],
         "event_at": event_at,
         "tags": [f"src:{ctx.source_tag}", f"project:{project}", "scope:shared",
                  "trust:agent", "sensitivity:internal", f"type:{args['type']}"]},
        args["retrieval_text"], detail)


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
               "event_at": event_at, "project": project, "type": args["type"]}
    phmac = canonical.payload_hmac(env.idem_key, cfields)
    # Reservations written before event_at was normalized ahead of the HMAC
    # carry a hash over the caller's raw argument, so accept and migrate that
    # one provable spelling; otherwise the upgrade would turn every existing
    # retry into a conflict.  Equivalent-but-differently-spelled timestamps are
    # deliberately not enumerated: before normalization they hashed differently
    # and already conflicted, so there is no behaviour to preserve, and the
    # stored HMAC cannot prove which canonical field actually changed.
    compatible_phmacs = {phmac}
    legacy_event_values = {args.get("event_at")}
    if event_at == "unset":
        legacy_event_values.update((None, "unset"))
    for legacy_event_at in legacy_event_values:
        legacy_fields = dict(cfields, event_at=legacy_event_at)
        compatible_phmacs.add(canonical.payload_hmac(env.idem_key, legacy_fields))
    conn = ctx.conn

    failpoints.hit("commit.reserve.before")
    sm.begin_immediate(conn)
    try:
        res = sm.lookup_reservation(conn, ctx.client_id, args["idempotency_key"])
        if res is not None:
            if (res["canonical_version"] != C.CANONICAL_VERSION
                    or res["payload_hmac"] not in compatible_phmacs):
                conn.execute("COMMIT")
                audit.finalize(conn, ctx.request_id, "rejected", "idempotency_conflict")
                ctx.audit_done = True
                raise ToolError(C.E_IDEMPOTENCY_CONFLICT, http_status=409)
            if res["payload_hmac"] != phmac:
                conn.execute(
                    "UPDATE idempotency_reservation SET payload_hmac=? WHERE event_id=?",
                    (phmac, res["event_id"]),
                )
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
    md = _render_commit_document(ctx, res, args, detail, event_at, project)
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
            staging_present = staging.exists() or staging.is_symlink()
            if staging_present:
                try:
                    vault.refuse_symlink(staging)
                    staging_valid = vault.artifact_matches(staging, desired_sha)
                except vault.VaultError:
                    staging_valid = False
                if not staging_valid:
                    sm.set_reservation_state(conn, res["event_id"], "conflict",
                                             release_lease=True)
                    _finalize_quiet(conn, ctx, "error", C.E_HASH_MISMATCH)
                    raise ToolError(C.E_HASH_MISMATCH, state="error")
            else:
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
        prepared_committed = False
        try:
            failpoints.hit("commit.prepared.before")
            sm.begin_immediate(conn)
            sm.insert_outbox_prepared(conn, res, desired_sha, env.spv)
            sm.set_reservation_state(conn, res["event_id"], "prepared")
            audit.set_state(conn, ctx.request_id, "prepared")
            conn.execute("COMMIT")
            prepared_committed = True
            failpoints.hit("commit.prepared.after")
        except BaseException:
            if prepared_committed:
                _finalize_quiet(conn, ctx, "recovery_pending", "prepared_followup_failed")
                return {"accepted": True, "stored": False, "indexed": False,
                        "index_state": "recovery_pending", "document_id": res["document_id"],
                        "uri": res["uri"]}
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            staging.unlink(missing_ok=True)
            sm.set_reservation_state(conn, res["event_id"], "retryable_failed", release_lease=True)
            _finalize_quiet(conn, ctx, "error", "retryable_failed")
            raise ToolError(C.E_UNAVAILABLE, state="error", outcome="retryable_failed",
                            retryable=True) from None
    try:
        failpoints.hit("commit.rename.before")
        final_present = vault.artifact_exists(final)
        staging_present = vault.artifact_exists(staging)
        final_ok = final_present and vault.artifact_matches(final, desired_sha)
        staging_ok = staging_present and vault.artifact_matches(staging, desired_sha)
        if ((final_present and not final_ok)
                or (staging_present and not staging_ok)):
            sm.mark_conflict(conn, res["event_id"], C.E_HASH_MISMATCH)
            _finalize_quiet(conn, ctx, "error", C.E_HASH_MISMATCH)
            raise ToolError(C.E_HASH_MISMATCH, state="error")
        if final_ok:
            if staging_ok:
                vault.discard_staging(staging)
        elif staging_ok:
            vault.promote_staging(staging, final)
        else:
            sm.mark_durability_gap(conn, res["event_id"])
            env.health["durability_gap"] = True
            _finalize_quiet(conn, ctx, "error", C.E_DURABILITY_GAP)
            raise ToolError(C.E_DURABILITY_GAP, state="error")
        if not vault.artifact_matches(final, desired_sha):
            if vault.artifact_exists(final):
                sm.mark_conflict(conn, res["event_id"], C.E_HASH_MISMATCH)
                code = C.E_HASH_MISMATCH
            else:
                sm.mark_durability_gap(conn, res["event_id"])
                env.health["durability_gap"] = True
                code = C.E_DURABILITY_GAP
            _finalize_quiet(conn, ctx, "error", code)
            raise ToolError(code, state="error")
        failpoints.hit("commit.rename.after")
    except ToolError:
        raise
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


def _finalize_quiet(conn, ctx, state: str, outcome: str) -> bool:
    try:
        audit.finalize(conn, ctx.request_id, state, outcome)
        ctx.audit_done = True
        return True
    except AuditError:
        return False


# ---------------------------------------------------------------- memory_search

def _valid_recall_event_at(value: str) -> bool:
    if value == "unset":
        return True
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _recall_score(fact: dict) -> float | None:
    scores = fact.get("scores")
    value = scores.get("final") if isinstance(scores, dict) else fact.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score if math.isfinite(score) else None


def _search_provenance_matches(
        env, conn, res, out, document_id: str, uri: str, source_agent: str) -> bool:
    if res is None or out is None:
        return False
    rows_match = (
        res["state"] == "stored"
        and out["status"] == "indexed"
        and res["event_id"] == out["event_id"]
        and res["root_request_id"] == out["root_request_id"]
        and res["client_id"] == out["client_id"]
        and res["idempotency_key"] == out["idempotency_key"]
        and res["server_bank_id"] == env.bank == out["server_bank_id"]
        and res["document_id"] == document_id == out["document_id"]
        and res["uri"] == uri == out["uri"]
    )
    if not rows_match:
        return False
    client = conn.execute(
        "SELECT source_tag FROM clients WHERE client_id=?", (res["client_id"],)).fetchone()
    return client is not None and client["source_tag"] == source_agent


def memory_search(env, ctx, args: dict) -> dict:
    _strict_schema(args, {"query": str, "project": str}, {"type": str, "source": str})
    if textlimits.scalar_len(args["query"]) > C.MAX_QUERY_SCALARS:
        raise ToolError(C.E_LIMIT_EXCEEDED, fields=["query"])
    if not vault.PROJECT_RE.match(args["project"]):
        raise ToolError(C.E_SCHEMA_REJECTED, fields=["project"])
    scan_or_reject(env, {"query": args["query"]})
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
        env.index_backend_down = True
        raise ToolError(C.E_INDEX_UNAVAILABLE, state="error", outcome="index_unavailable",
                        retryable=True) from None
    env.index_backend_down = False

    groups: dict[str, dict] = {}
    results = raw.get("results", []) if isinstance(raw, dict) else []
    if not isinstance(results, list):
        results = []
    known_sources = env.known_source_tags()
    for fact in results:
        if not isinstance(fact, dict):
            continue
        doc = fact.get("document_id")
        if not isinstance(doc, str):
            continue
        group = groups.setdefault(doc, {
            "facts": [], "bad": False, "metas": set(), "tagsets": set(),
        })
        meta = fact.get("metadata")
        tags = fact.get("tags")
        text = fact.get("text") or fact.get("content")
        score = _recall_score(fact)
        required_meta = (
            "uri", "project", "source_agent", "event_at", "index_title", "index_summary",
            "trust", "sensitivity", "retrieval_sha256",
        )
        if (not isinstance(meta, dict) or not isinstance(tags, list)
                or not all(isinstance(tag, str) for tag in tags)
                or len(tags) > C.MAX_ARRAY_ITEMS or len(tags) != len(set(tags))
                or not isinstance(text, str) or score is None
                or not all(isinstance(meta.get(name), str) for name in required_meta)):
            group["bad"] = True
            continue

        project_tags = [tag for tag in tags if tag.startswith("project:")]
        source_tags = [tag for tag in tags if tag.startswith("src:")]
        type_tags = [tag for tag in tags if tag.startswith("type:")]
        try:
            uproj, udoc = vault.parse_uri(meta["uri"])
        except vault.VaultError:
            group["bad"] = True
            continue
        if (
            project_tags != [f"project:{args['project']}"]
            or source_tags != [f"src:{meta['source_agent']}"]
            or len(type_tags) != 1 or type_tags[0][5:] not in C.TYPES
            or meta["project"] != args["project"] or uproj != args["project"] or udoc != doc
            or meta["source_agent"] not in known_sources
            or meta["trust"] != "agent" or meta["sensitivity"] != "internal"
            or "scope:shared" not in tags or "trust:agent" not in tags
            or "sensitivity:internal" not in tags
            or not _valid_recall_event_at(meta["event_at"])
            or textlimits.scalar_len(meta["index_title"]) > C.MAX_TITLE_SCALARS
            or not (C.SUMMARY_MIN_SCALARS
                    <= textlimits.scalar_len(meta["index_summary"])
                    <= C.SUMMARY_MAX_SCALARS)
            or re.fullmatch(r"[0-9a-f]{64}", meta["retrieval_sha256"]) is None
            or ("type" in args and f"type:{args['type']}" not in tags)
            or (source is not None and f"src:{source}" not in tags)
        ):
            group["bad"] = True
            continue

        returned_strings = [
            doc, meta["uri"], meta["index_title"], meta["index_summary"], text,
            *tags, meta["source_agent"], meta["trust"], meta["event_at"],
        ]
        if any(env.scan_text(value) for value in returned_strings):
            group["bad"] = True
            continue
        group["facts"].append({"text": text, "score": score, "meta": meta, "tags": tags})
        group["metas"].add(tuple((name, meta[name]) for name in required_meta))
        group["tagsets"].add(tuple(sorted(tags)))

    cards = []
    for doc, group in groups.items():
        if (group["bad"] or not group["facts"] or len(group["metas"]) != 1
                or len(group["tagsets"]) != 1):
            continue
        best = max(group["facts"], key=lambda fact: fact["score"])
        meta = best["meta"]
        res_row, out_row = sm.status_by_ref(ctx.conn, document_id=doc)
        if not _search_provenance_matches(
                env, ctx.conn, res_row, out_row, doc, meta["uri"], meta["source_agent"]):
            continue
        cards.append({
            "score": best["score"],
            "card": {
                "document_id": doc, "uri": meta["uri"],
                "index_title": meta["index_title"], "index_summary": meta["index_summary"],
                "safe_snippet": best["text"][:512],
                "tags": sorted(best["tags"]), "source_agent": meta["source_agent"],
                "trust": meta["trust"], "event_at": meta["event_at"],
                "index_state": C.index_state_projection(out_row["status"], res_row["state"]),
            },
        })
    cards.sort(key=lambda c: c["score"], reverse=True)
    return {"results": [c["card"] for c in cards[:C.SEARCH_MAX_CARDS]]}


# ------------------------------------------------------------------ memory_read

def _read_reservation_matches(env, res, document_id: str, uri: str) -> bool:
    return (
        res is not None
        and res["server_bank_id"] == env.bank
        and res["document_id"] == document_id
        and res["uri"] == uri
    )


def _read_outbox_matches(env, res, out, document_id: str, uri: str) -> bool:
    return (
        out is not None
        and out["event_id"] == res["event_id"]
        and out["root_request_id"] == res["root_request_id"]
        and out["client_id"] == res["client_id"]
        and out["idempotency_key"] == res["idempotency_key"]
        and out["server_bank_id"] == env.bank
        and out["document_id"] == document_id
        and out["uri"] == uri
    )


def _mark_read_hash_conflict(conn, event_id: str) -> None:
    sm.mark_conflict(conn, event_id, C.E_HASH_MISMATCH)


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
        res_row, out_row = sm.status_by_ref(ctx.conn, document_id=doc_id)
        if not _read_reservation_matches(env, res_row, doc_id, uri):
            raise ToolError(C.E_NOT_FOUND)
        if not _read_outbox_matches(env, res_row, out_row, doc_id, uri):
            raise ToolError(C.E_STATE_INVARIANT)
        if out_row["status"] in ("policy_blocked", "conflict"):
            raise ToolError(C.E_POLICY_BLOCKED if out_row["status"] == "policy_blocked"
                            else C.E_STATE_INVARIANT)
        if (res_row["state"] != "stored"
                or out_row["status"] not in ("ready", "indexing", "retry_wait",
                                             "indexed", "dead")):
            raise ToolError(C.E_STATE_INVARIANT)
        try:
            path = vault.doc_path(env.vault_root, project, doc_id)
            data = vault.read_artifact(path, max_bytes=C.MAX_READ_DOC_BYTES)
        except vault.VaultError as exc:
            if exc.code == "ARTIFACT_MISSING":
                sm.mark_durability_gap(ctx.conn, out_row["event_id"])
                env.health["durability_gap"] = True
                raise ToolError(C.E_NOT_FOUND) from None
            _mark_read_hash_conflict(ctx.conn, out_row["event_id"])
            raise ToolError(C.E_STATE_INVARIANT, outcome=C.E_HASH_MISMATCH) from None
        if vault.sha256_bytes(data) != out_row["desired_sha256"]:
            _mark_read_hash_conflict(ctx.conn, out_row["event_id"])
            raise ToolError(C.E_STATE_INVARIANT, outcome=C.E_HASH_MISMATCH)
        decoded = data.decode("utf-8", errors="replace")
        if out_row["scan_policy_version"] != env.spv:
            if env.scan_text(decoded):
                sm.set_outbox_status(ctx.conn, out_row["event_id"], "policy_blocked",
                                     error_code=C.E_POLICY_BLOCKED, release_lease=True)
                ctx.conn.execute("UPDATE outbox SET scan_policy_version=? WHERE event_id=?",
                                 (env.spv, out_row["event_id"]))
                raise ToolError(C.E_POLICY_BLOCKED)
            ctx.conn.execute("UPDATE outbox SET scan_policy_version=? WHERE event_id=?",
                             (env.spv, out_row["event_id"]))
        total += len(data)
        if total > C.MAX_READ_TOTAL_BYTES:
            raise ToolError(C.E_LIMIT_EXCEEDED, fields=["uris"])
        docs.append({"uri": uri, "markdown": decoded})
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

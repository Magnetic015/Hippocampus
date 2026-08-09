"""Startup recovery (plan 05 §5.3) + stale-audit reconciliation (07 §7.6)."""

from __future__ import annotations

from pathlib import Path

from . import audit, constants as C, statemachine as sm, vault
from .db import connect, now


def run_startup_recovery(env) -> dict:
    stats = {"released_leases": 0, "promoted": 0, "readied": 0, "policy_blocked": 0,
             "hash_mismatch": 0, "durability_gap": 0, "unowned_orphan": 0,
             "invariant_broken": 0, "audit_converged": 0}
    conn = connect(env.db_path)
    try:
        cur = conn.execute(
            "UPDATE idempotency_reservation SET lease_owner=NULL, lease_expires_at=NULL"
            " WHERE state IN ('reserved','retryable_failed') AND lease_owner IS NOT NULL"
            " AND lease_expires_at < ?", (now(),))
        stats["released_leases"] = cur.rowcount

        for res in conn.execute(
                "SELECT * FROM idempotency_reservation WHERE state='prepared'").fetchall():
            if env.scanner_down:
                break  # scanner unavailable: recovery pauses, queue untouched
            out = conn.execute("SELECT * FROM outbox WHERE event_id=?",
                               (res["event_id"],)).fetchone()
            if out is None or out["status"] != "prepared":
                continue
            try:
                project, doc_id = vault.parse_uri(res["uri"])
                staging = vault.staging_path(env.vault_root, project, res["event_id"])
                final = vault.doc_path(env.vault_root, project, doc_id)
            except vault.VaultError:
                sm.mark_conflict(conn, res["event_id"], C.E_HASH_MISMATCH)
                stats["hash_mismatch"] += 1
                continue
            final_exists = _artifact_exists(final)
            staging_exists = _artifact_exists(staging)
            final_ok = final_exists and vault.artifact_matches(final, out["desired_sha256"])
            staging_ok = staging_exists and vault.artifact_matches(staging, out["desired_sha256"])
            if (final_exists and not final_ok) or (staging_exists and not staging_ok):
                # bytes present but altered: never guess or overwrite (05 §5.3)
                sm.mark_conflict(conn, res["event_id"], C.E_HASH_MISMATCH)
                stats["hash_mismatch"] += 1
                continue
            if final_ok:
                if env.scan_text(final.read_text("utf-8")):
                    sm.set_outbox_status(conn, res["event_id"], "policy_blocked",
                                         error_code="POLICY_BLOCKED", release_lease=True)
                    stats["policy_blocked"] += 1
                    continue
                if staging_ok:
                    vault.discard_staging(staging)
                _mark_stored_ready(conn, res["event_id"])
                stats["readied"] += 1
            elif staging_ok:
                if env.scan_text(staging.read_text("utf-8")):
                    sm.set_outbox_status(conn, res["event_id"], "policy_blocked",
                                         error_code="POLICY_BLOCKED", release_lease=True)
                    stats["policy_blocked"] += 1
                    continue
                vault.promote_staging(staging, final)
                _mark_stored_ready(conn, res["event_id"])
                stats["promoted"] += 1
            else:
                sm.mark_durability_gap(conn, res["event_id"])
                env.health["durability_gap"] = True
                stats["durability_gap"] += 1

        for res in conn.execute(
                "SELECT r.event_id FROM idempotency_reservation r"
                " LEFT JOIN outbox o ON o.event_id = r.event_id"
                " WHERE r.state='stored' AND o.event_id IS NULL").fetchall():
            env.health["invariant_broken"] = True
            stats["invariant_broken"] += 1

        # Audit reconciliation must observe the artifact state after prepared
        # reservations have converged.  In particular, a prior process may have
        # truthfully left a request at recovery_pending before its staging file
        # is promoted above.
        stats["audit_converged"] = audit.reconcile_startup(conn, env.pid)
        stats["unowned_orphan"] = _count_orphans(env, conn)
    finally:
        conn.close()
    return stats


def _artifact_exists(path: Path) -> bool:
    return vault.artifact_exists(path)


def _mark_stored_ready(conn, event_id: str) -> None:
    sm.begin_immediate(conn)
    try:
        sm.set_outbox_status(conn, event_id, "ready", release_lease=True)
        sm.set_reservation_state(conn, event_id, "stored", release_lease=True)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _count_orphans(env, conn) -> int:
    known_docs = {r["document_id"] for r in conn.execute(
        "SELECT document_id FROM idempotency_reservation")}
    known_events = {r["event_id"] for r in conn.execute(
        "SELECT event_id FROM idempotency_reservation")}
    orphans = 0
    shared = Path(env.vault_root) / "shared"
    if not shared.exists():
        return 0
    for path in shared.glob("*/*"):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(".md") and name[:-3] in known_docs:
            continue
        if name.startswith(".hippocampus-") and name.endswith(".staging") \
                and name[len(".hippocampus-"):-len(".staging")] in known_events:
            continue
        orphans += 1  # counted only; never read, indexed, adopted or deleted
    return orphans

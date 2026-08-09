"""Regression coverage for bounded age/cap audit retention and its scheduler."""

from harness import Lab

from hippocampus import audit, constants as C
from hippocampus.db import connect, init_db


def _open_db(tmp_path):
    path = tmp_path / "state" / "outbox.db"
    init_db(str(path))
    return connect(str(path))


def _insert_audit(conn, request_id: str, ts: int) -> None:
    conn.execute(
        "INSERT INTO audit (request_id, ts, process_instance_id, state, outcome_code)"
        " VALUES (?, ?, 'retention-test', 'ok', 'test')",
        (request_id, ts),
    )


def test_retention_purges_only_rows_older_than_ninety_days(tmp_path):
    current_ts = 2_000_000_000
    cutoff = current_ts - C.AUDIT_RETENTION_DAYS * 86400
    conn = _open_db(tmp_path)
    try:
        _insert_audit(conn, "expired-2", cutoff - 2)
        _insert_audit(conn, "expired-1", cutoff - 1)
        _insert_audit(conn, "boundary", cutoff)
        _insert_audit(conn, "recent", current_ts)

        deleted = audit.enforce_retention(
            conn, current_ts=current_ts, max_rows=100, batch_size=10)
        remaining = [row["request_id"] for row in conn.execute(
            "SELECT request_id FROM audit ORDER BY ts, rowid")]
    finally:
        conn.close()

    assert deleted == 2
    assert remaining == ["boundary", "recent"]


def test_retention_row_cap_removes_oldest_rows_in_order(tmp_path):
    current_ts = 2_000_000_000
    conn = _open_db(tmp_path)
    try:
        for index in range(5):
            _insert_audit(conn, f"row-{index}", current_ts - 10 + index)

        deleted = audit.enforce_retention(
            conn, current_ts=current_ts, max_rows=3, batch_size=10)
        remaining = [row["request_id"] for row in conn.execute(
            "SELECT request_id FROM audit ORDER BY ts, rowid")]
    finally:
        conn.close()

    assert deleted == 2
    assert remaining == ["row-2", "row-3", "row-4"]


def test_retention_converges_when_excess_exceeds_one_batch(tmp_path):
    current_ts = 2_000_000_000
    conn = _open_db(tmp_path)
    try:
        for index in range(25):
            _insert_audit(conn, f"row-{index:02d}", current_ts - 100 + index)

        deleted = audit.enforce_retention(
            conn, current_ts=current_ts, max_rows=5, batch_size=3,
            max_batches=10, run_budget_s=60)
        remaining = [row["request_id"] for row in conn.execute(
            "SELECT request_id FROM audit ORDER BY ts, rowid")]
    finally:
        conn.close()

    assert deleted == 20
    assert remaining == ["row-20", "row-21", "row-22", "row-23", "row-24"]


def test_retention_batch_is_bounded_and_ordered(tmp_path):
    current_ts = 2_000_000_000
    cutoff = current_ts - C.AUDIT_RETENTION_DAYS * 86400
    conn = _open_db(tmp_path)
    try:
        for index in range(5):
            _insert_audit(conn, f"row-{index}", cutoff - 5 + index)

        deleted = audit.purge_retention_batch(
            conn, cutoff_ts=cutoff, max_rows=100, batch_size=2)
        remaining = [row["request_id"] for row in conn.execute(
            "SELECT request_id FROM audit ORDER BY ts, rowid")]
    finally:
        conn.close()

    assert deleted == 2
    assert remaining == ["row-2", "row-3", "row-4"]


def test_retention_run_has_a_finite_batch_budget(tmp_path):
    current_ts = 2_000_000_000
    cutoff = current_ts - C.AUDIT_RETENTION_DAYS * 86400
    conn = _open_db(tmp_path)
    try:
        for index in range(5):
            _insert_audit(conn, f"row-{index}", cutoff - 5 + index)

        try:
            audit.enforce_retention(
                conn, current_ts=current_ts, max_rows=100, batch_size=1,
                max_batches=2, run_budget_s=60)
            raise AssertionError("retention exceeded its configured batch budget")
        except audit.AuditError:
            pass
        remaining = [row["request_id"] for row in conn.execute(
            "SELECT request_id FROM audit ORDER BY ts, rowid")]
    finally:
        conn.close()

    assert remaining == ["row-2", "row-3", "row-4"]


def test_retention_never_deletes_active_or_unreconciled_rows(tmp_path):
    current_ts = 2_000_000_000
    old_ts = current_ts - C.AUDIT_RETENTION_DAYS * 86400 - 1
    protected_states = ("started", "prepared", "stored_pending", "recovery_pending")
    conn = _open_db(tmp_path)
    try:
        for state in protected_states:
            _insert_audit(conn, state, old_ts)
            conn.execute("UPDATE audit SET state=? WHERE request_id=?", (state, state))
        _insert_audit(conn, "terminal", old_ts)

        deleted = audit.enforce_retention(
            conn, current_ts=current_ts, max_rows=10, batch_size=10)
        remaining = {row["request_id"] for row in conn.execute("SELECT request_id FROM audit")}
    finally:
        conn.close()

    assert deleted == 1
    assert remaining == set(protected_states)


def test_retention_reports_cap_blocked_instead_of_deleting_active_rows(tmp_path):
    current_ts = 2_000_000_000
    conn = _open_db(tmp_path)
    try:
        for state in ("started", "recovery_pending"):
            _insert_audit(conn, state, current_ts)
            conn.execute("UPDATE audit SET state=? WHERE request_id=?", (state, state))

        try:
            audit.enforce_retention(
                conn, current_ts=current_ts, max_rows=1, batch_size=10)
            raise AssertionError("protected rows incorrectly satisfied the audit cap")
        except audit.AuditError:
            pass
        remaining = {row["request_id"] for row in conn.execute("SELECT request_id FROM audit")}
    finally:
        conn.close()

    assert remaining == {"started", "recovery_pending"}


def test_server_runs_retention_at_startup_and_at_most_hourly(tmp_path, monkeypatch):
    calls = []
    real_enforce = audit.enforce_retention

    def recording_enforce(conn):
        calls.append(True)
        return real_enforce(conn)

    monkeypatch.setattr(audit, "enforce_retention", recording_enforce)
    lab = Lab(tmp_path, with_worker=False)
    try:
        assert len(calls) == 1  # build_server startup path
        last = lab.env._audit_retention_last_attempt
        assert last is not None
        assert lab.env.maintain_audit_retention(
            monotonic_now=last + C.AUDIT_RETENTION_INTERVAL_S - 1)
        assert len(calls) == 1
        assert lab.env.maintain_audit_retention(
            monotonic_now=last + C.AUDIT_RETENTION_INTERVAL_S)
        assert len(calls) == 2
    finally:
        lab.close()


def test_retention_failure_marks_readiness_down_without_leaking_error(
        tmp_path, monkeypatch):
    marker = "private-retention-failure-detail"
    attempts = []

    def fail_retention(_conn):
        attempts.append(True)
        raise RuntimeError(marker)

    monkeypatch.setattr(audit, "enforce_retention", fail_retention)
    lab = Lab(tmp_path, with_worker=False)
    try:
        assert lab.env.health["audit_retention_failed"] is True
        assert lab.env.health["audit_sink_down"] is True
        assert lab.env._audit_retention_last_attempt is not None
        # Pin the gate to an exactly representable instant. Reusing the real
        # time.monotonic() reading makes `last + interval` round, so the gate's
        # `attempt_at - last` can land a few ulps under the interval and skip
        # the due attempt -- around 7% of the time on a freshly booted host,
        # where monotonic() is still small, and effectively never on a
        # long-running one.
        last = 1000.0
        lab.env._audit_retention_last_attempt = last
        assert not lab.env.maintain_audit_retention(
            monotonic_now=last + C.AUDIT_RETENTION_RETRY_INTERVAL_S - 1)
        assert len(attempts) == 1
        assert not lab.env.maintain_audit_retention(
            monotonic_now=last + C.AUDIT_RETENTION_RETRY_INTERVAL_S)
        assert len(attempts) == 2
        status, body = lab.raw(
            b"", method="GET", url=lab.url.replace("/mcp/", "/readyz"))
        assert status == 503 and body == {"status": "down"}
        assert marker not in repr(body)
    finally:
        lab.close()

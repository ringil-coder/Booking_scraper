from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from booking_monitor.models import CycleStatus, JobStatus
from booking_monitor.storage.batches import BatchIntegrityError, write_batch
from booking_monitor.storage.database import (
    apply_event,
    backup_database,
    from_db_timestamp,
    import_batch,
    import_manifest,
    mark_batch_imported,
    open_database,
    set_cycle_status,
    to_db_timestamp,
)
from booking_monitor.storage.layout import DataLayout
from booking_monitor.storage.state import BlockInfo, CycleProgress
from tests.storage_helpers import (
    RUN,
    T0,
    attempt_event,
    expired_event,
    make_manifest,
    planned_for,
)


@pytest.fixture
def db():
    conn = open_database(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def cycle(db):
    manifest = make_manifest()
    planned = planned_for(manifest)
    import_manifest(db, manifest, planned)
    return manifest, planned


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _job(conn, manifest, planned):
    return conn.execute(
        "SELECT * FROM jobs WHERE id = ?", (f"{manifest.cycle_id}:{planned.query.query_id}",)
    ).fetchone()


def test_timestamps_have_fixed_width_and_roundtrip():
    a = to_db_timestamp(datetime(2026, 11, 4, 5, 17, tzinfo=UTC))
    b = to_db_timestamp(datetime(2026, 11, 4, 5, 17, 0, 500, tzinfo=UTC))
    assert a == "2026-11-04T05:17:00.000000+00:00"
    assert len(a) == len(b) and a < b
    assert from_db_timestamp(a) == datetime(2026, 11, 4, 5, 17, tzinfo=UTC)
    with pytest.raises(ValueError):
        to_db_timestamp(datetime(2026, 11, 4))


def test_open_database_on_disk_creates_parent(tmp_path):
    conn = open_database(tmp_path / ".runtime" / "monitor.sqlite3")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_import_manifest_creates_cycle_queries_and_jobs(db, cycle):
    manifest, planned = cycle
    assert _count(db, "cycles") == 1
    assert _count(db, "competitors") == 2
    assert _count(db, "search_profiles") == 1
    assert _count(db, "cycle_competitors") == 2
    assert _count(db, "stay_queries") == manifest.query_count == 30 * 3 * 2
    assert _count(db, "jobs") == manifest.query_count
    statuses = {row[0] for row in db.execute("SELECT DISTINCT status FROM jobs")}
    assert statuses == {"pending"}
    row = db.execute("SELECT status, stay_nights FROM cycles").fetchone()
    assert row["status"] == "planned" and row["stay_nights"] == "[2, 3, 4]"
    first = db.execute("SELECT query_id FROM jobs ORDER BY sort_key LIMIT 1").fetchone()
    assert first["query_id"] == planned[0].query.query_id


def test_import_manifest_is_idempotent(db, cycle):
    manifest, planned = cycle
    import_manifest(db, manifest, planned)
    assert _count(db, "jobs") == manifest.query_count
    assert _count(db, "competitors") == 2


def test_import_manifest_rejects_different_manifest_for_same_cycle(db, cycle):
    manifest, planned = cycle
    other = manifest.model_copy(update={"config_hash": "cfg_inny"})
    with pytest.raises(ValueError, match="innym manifestem"):
        import_manifest(db, other, planned)


def test_queries_shared_between_cycles(db, cycle):
    manifest, _ = cycle
    second = make_manifest(slot=1, cycle_date=manifest.cycle_date)
    second = second.model_copy(update={"cycle_id": "c20261104-s0001-test"})
    import_manifest(db, second, planned_for(second))
    assert _count(db, "jobs") == 2 * manifest.query_count
    assert _count(db, "stay_queries") == manifest.query_count


def test_apply_attempt_with_offers_and_components(db, cycle):
    manifest, planned = cycle
    apply_event(db, attempt_event(manifest, planned[0], offers=2), batch_id="b1")
    job = _job(db, manifest, planned[0])
    assert job["status"] == "completed" and job["attempts"] == 1
    assert _count(db, "attempts") == 1
    assert _count(db, "observations") == 1
    assert _count(db, "offers") == 2
    assert _count(db, "price_components") == 4
    offer = db.execute("SELECT * FROM offers ORDER BY offer_index LIMIT 1").fetchone()
    assert offer["total_price_grosze"] == 64_000
    assert offer["discounts"] == '["Oferta specjalna"]'
    assert offer["selected_for_category"] == 1 and offer["deposit_grosze"] == 50_000
    vat = db.execute("SELECT * FROM price_components WHERE kind = 'vat' LIMIT 1").fetchone()
    assert vat["rate_percent"] == "8" and vat["included"] == 1
    attempt = db.execute("SELECT * FROM attempts").fetchone()
    assert attempt["batch_id"] == "b1" and attempt["workflow_run_id"] == "123"


def test_retry_then_success_updates_job(db, cycle):
    manifest, planned = cycle
    apply_event(db, attempt_event(manifest, planned[0], status=JobStatus.RETRY_WAIT))
    job = _job(db, manifest, planned[0])
    assert job["status"] == "retry_wait"
    assert job["last_reason"] == "timeout" and job["next_attempt_at"] is not None
    apply_event(db, attempt_event(manifest, planned[0], attempt_no=2))
    job = _job(db, manifest, planned[0])
    assert job["status"] == "completed" and job["attempts"] == 2
    assert job["next_attempt_at"] is None and job["last_reason"] is None


def test_duplicate_attempt_is_rejected(db, cycle):
    manifest, planned = cycle
    event = attempt_event(manifest, planned[0])
    apply_event(db, event)
    with pytest.raises(sqlite3.IntegrityError):
        apply_event(db, event)
    # Nieudana transakcja nie zostawia częściowych zapisów.
    assert _count(db, "attempts") == 1 and _count(db, "offers") == 1


def test_event_for_unknown_job_rejected(db):
    manifest = make_manifest()
    with pytest.raises(ValueError, match="nieznanego zadania"):
        apply_event(db, attempt_event(manifest, planned_for(manifest)[0]))


def test_expired_event(db, cycle):
    manifest, planned = cycle
    apply_event(db, expired_event(manifest, planned[1]))
    assert _job(db, manifest, planned[1])["status"] == "expired"
    apply_event(db, attempt_event(manifest, planned[0]))
    with pytest.raises(ValueError, match="ukończone"):
        apply_event(db, expired_event(manifest, planned[0]))


def test_import_batch_is_idempotent(tmp_path, db, cycle):
    manifest, planned = cycle
    layout = DataLayout(tmp_path)
    events = [attempt_event(manifest, p) for p in planned[:3]]
    ref = write_batch(layout, manifest.cycle_id, "b1", events, created_at=T0, run=RUN)
    assert import_batch(db, layout, manifest.cycle_id, ref) is True
    assert import_batch(db, layout, manifest.cycle_id, ref) is False
    assert _count(db, "attempts") == 3
    assert _count(db, "imported_batches") == 1
    batch_ids = {row[0] for row in db.execute("SELECT DISTINCT batch_id FROM attempts")}
    assert batch_ids == {"b1"}


def test_import_batch_with_same_id_and_other_checksum_rejected(tmp_path, db, cycle):
    manifest, planned = cycle
    layout = DataLayout(tmp_path)
    ref = write_batch(
        layout,
        manifest.cycle_id,
        "b1",
        [attempt_event(manifest, planned[0])],
        created_at=T0,
        run=RUN,
    )
    import_batch(db, layout, manifest.cycle_id, ref)
    with pytest.raises(BatchIntegrityError):
        import_batch(db, layout, manifest.cycle_id, ref.model_copy(update={"sha256": "0" * 64}))


def test_import_batch_is_atomic(tmp_path, db, cycle):
    manifest, planned = cycle
    layout = DataLayout(tmp_path)
    apply_event(db, attempt_event(manifest, planned[1]))
    events = [attempt_event(manifest, planned[0]), attempt_event(manifest, planned[1])]
    ref = write_batch(layout, manifest.cycle_id, "b1", events, created_at=T0, run=RUN)
    with pytest.raises(sqlite3.IntegrityError):
        import_batch(db, layout, manifest.cycle_id, ref)
    assert _job(db, manifest, planned[0])["status"] == "pending"
    assert _count(db, "imported_batches") == 0


def test_mark_batch_imported_after_live_apply(tmp_path, db, cycle):
    manifest, planned = cycle
    layout = DataLayout(tmp_path)
    events = [attempt_event(manifest, p) for p in planned[:2]]
    for event in events:
        apply_event(db, event)
    ref = write_batch(layout, manifest.cycle_id, "b1", events, created_at=T0, run=RUN)
    mark_batch_imported(db, manifest.cycle_id, ref, events)
    mark_batch_imported(db, manifest.cycle_id, ref, events)
    assert import_batch(db, layout, manifest.cycle_id, ref) is False
    assert {row[0] for row in db.execute("SELECT batch_id FROM attempts")} == {"b1"}
    with pytest.raises(BatchIntegrityError):
        mark_batch_imported(db, manifest.cycle_id, ref.model_copy(update={"sha256": "1" * 64}))


def test_set_cycle_status(db, cycle):
    manifest, _ = cycle
    progress = CycleProgress(
        cycle_id=manifest.cycle_id,
        status=CycleStatus.BLOCKED,
        started_at=T0,
        blocked=BlockInfo(at=T0, reason="blocked", message="CAPTCHA"),
        updated_at=T0,
    )
    set_cycle_status(db, manifest.cycle_id, progress)
    row = db.execute("SELECT * FROM cycles").fetchone()
    assert row["status"] == "blocked" and row["blocked_message"] == "CAPTCHA"
    assert row["started_at"] == to_db_timestamp(T0)
    with pytest.raises(ValueError):
        set_cycle_status(db, "c-nieznany", progress.model_copy(update={"cycle_id": "c-nieznany"}))


def test_backup_database(tmp_path, db, cycle):
    manifest, planned = cycle
    apply_event(db, attempt_event(manifest, planned[0]))
    dest = tmp_path / "out" / "monitor.sqlite3"
    backup_database(db, dest)
    backup_database(db, dest)
    copy = sqlite3.connect(dest)
    try:
        assert copy.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1
        assert copy.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == manifest.query_count
    finally:
        copy.close()

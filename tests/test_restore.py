"""Odtwarzanie bazy z gałęzi danych (A§8; A§15 pkt 5 i 6 na poziomie warstwy trwałej)."""

from __future__ import annotations

import json
import shutil
from datetime import date, timedelta

import pytest

from booking_monitor.models import CycleStatus, JobStatus
from booking_monitor.storage.batches import write_batch
from booking_monitor.storage.database import apply_event, open_database
from booking_monitor.storage.files import write_model
from booking_monitor.storage.init import initialize_data_dir
from booking_monitor.storage.layout import DataLayout
from booking_monitor.storage.restore import (
    RestoreError,
    SchemaMismatchError,
    StateMissingError,
    check_schema_version,
    load_cycle,
    load_scheduler_state,
    rebuild_database,
)
from booking_monitor.storage.state import CycleProgress
from tests.storage_helpers import (
    RUN,
    T0,
    attempt_event,
    expired_event,
    init_layout,
    make_config,
    make_manifest,
    planned_for,
    write_cycle,
)

NOW = T0 + timedelta(days=4)


def _counts(conn) -> dict[str, int]:
    tables = [
        "cycles",
        "stay_queries",
        "jobs",
        "attempts",
        "observations",
        "offers",
        "price_components",
        "imported_batches",
    ]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def _job_statuses(conn, cycle_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE cycle_id = ? GROUP BY status", (cycle_id,)
    )
    return {row["status"]: row["n"] for row in rows}


@pytest.fixture
def two_cycles(tmp_path):
    layout = init_layout(tmp_path)
    first = make_manifest(date(2026, 11, 4), slot=0)
    second = make_manifest(date(2026, 11, 7), slot=1)
    p1, p2 = planned_for(first), planned_for(second)
    write_cycle(
        layout,
        first,
        [
            [attempt_event(first, p1[0], offers=2), attempt_event(first, p1[1], offers=0)],
            [
                attempt_event(first, p1[2], status=JobStatus.RETRY_WAIT),
                expired_event(first, p1[3]),
            ],
            [
                attempt_event(first, p1[2], attempt_no=2),
                attempt_event(first, p1[4], status=JobStatus.FAILED),
            ],
        ],
        status=CycleStatus.COMPLETED_WITH_ERRORS,
    )
    write_cycle(layout, second, [[attempt_event(second, p2[0])]], status=CycleStatus.RUNNING)
    return layout, first, second


def test_rebuild_two_cycles(two_cycles):
    layout, first, second = two_cycles
    conn = open_database(":memory:")
    report = rebuild_database(layout, conn, now=NOW, retention_days=365)
    assert report.cycles == [first.cycle_id, second.cycle_id]
    assert report.batches_imported == 4
    assert report.events == 7
    assert report.orphan_batches == []
    counts = _counts(conn)
    assert counts["cycles"] == 2
    assert counts["jobs"] == first.query_count + second.query_count
    assert counts["attempts"] == 6 and counts["observations"] == 4
    assert counts["offers"] == 4 and counts["price_components"] == 8
    assert _job_statuses(conn, first.cycle_id) == {
        "completed": 3,
        "expired": 1,
        "failed": 1,
        "pending": first.query_count - 5,
    }
    retried = conn.execute(
        "SELECT attempts, status FROM jobs WHERE query_id = ? AND cycle_id = ?",
        (planned_for(first)[2].query.query_id, first.cycle_id),
    ).fetchone()
    assert retried["attempts"] == 2 and retried["status"] == "completed"
    statuses = dict(conn.execute("SELECT id, status FROM cycles").fetchall())
    assert statuses == {first.cycle_id: "completed_with_errors", second.cycle_id: "running"}


def test_rebuild_is_idempotent(two_cycles):
    layout, *_ = two_cycles
    conn = open_database(":memory:")
    rebuild_database(layout, conn, now=NOW, retention_days=365)
    before = _counts(conn)
    report = rebuild_database(layout, conn, now=NOW, retention_days=365)
    assert report.batches_imported == 0
    assert _counts(conn) == before


def test_rebuild_selected_cycle_only(two_cycles):
    layout, _, second = two_cycles
    conn = open_database(":memory:")
    report = rebuild_database(
        layout, conn, now=NOW, retention_days=365, cycle_ids=[second.cycle_id]
    )
    assert report.cycles == [second.cycle_id]
    with pytest.raises(RestoreError, match="brak katalogów"):
        rebuild_database(layout, conn, now=NOW, retention_days=365, cycle_ids=["c-brak"])


def test_retention_skips_old_cycles(two_cycles):
    layout, first, second = two_cycles
    conn = open_database(":memory:")
    report = rebuild_database(layout, conn, now=T0 + timedelta(days=368), retention_days=365)
    assert report.skipped_cycles == [first.cycle_id]
    assert report.cycles == [second.cycle_id]


def test_orphan_batch_is_ignored(two_cycles):
    layout, _first, second = two_cycles
    orphan = [attempt_event(second, planned_for(second)[1])]
    write_batch(layout, second.cycle_id, "b-orphan", orphan, created_at=T0, run=RUN)
    conn = open_database(":memory:")
    report = rebuild_database(layout, conn, now=NOW, retention_days=365)
    assert report.orphan_batches == [f"{second.cycle_id}/batches/b-orphan.jsonl.gz"]
    assert _job_statuses(conn, second.cycle_id)["completed"] == 1


def test_artifacts_expired_rebuild_from_data_dir_only(tmp_path, two_cycles):
    """A§15 pkt 6: utrata bazy i artefaktów nie usuwa historii."""
    layout, *_ = two_cycles
    work = tmp_path / ".runtime"
    conn = open_database(work / "monitor.sqlite3")
    rebuild_database(layout, conn, now=NOW, retention_days=365)
    expected = _counts(conn)
    conn.close()
    shutil.rmtree(work)
    fresh = open_database(work / "monitor.sqlite3")
    rebuild_database(layout, fresh, now=NOW, retention_days=365)
    assert _counts(fresh) == expected


def test_runner_loss_before_checkpoint(tmp_path):
    """A§15 pkt 5: praca sprzed utraty runnera bez checkpointu jest ponawiana, bez duplikatów."""
    layout = init_layout(tmp_path)
    manifest = make_manifest()
    planned = planned_for(manifest)
    committed = [attempt_event(manifest, planned[0])]
    write_cycle(layout, manifest, [committed])

    # Runner stosuje na bieżąco kolejne próby, ale ginie przed zapisem partii.
    live = open_database(":memory:")
    rebuild_database(layout, live, now=NOW, retention_days=365)
    lost = [attempt_event(manifest, planned[1]), attempt_event(manifest, planned[2])]
    for event in lost:
        apply_event(live, event)
    assert _job_statuses(live, manifest.cycle_id)["completed"] == 3

    # Nowy runner odtwarza wyłącznie zatwierdzony stan: utracone zadania znów są pending.
    restored = open_database(":memory:")
    rebuild_database(layout, restored, now=NOW, retention_days=365)
    assert _job_statuses(restored, manifest.cycle_id)["completed"] == 1
    pending = {
        row["query_id"]
        for row in restored.execute("SELECT query_id FROM jobs WHERE status = 'pending'")
    }
    assert {e.query.query_id for e in lost} <= pending

    # Ponowione próby (te same numery) trafiają do nowej partii; import bez duplikatów.
    retried = [attempt_event(manifest, planned[1]), attempt_event(manifest, planned[2])]
    for event in retried:
        apply_event(restored, event)
    ref = write_batch(layout, manifest.cycle_id, "b-retry", retried, created_at=NOW, run=RUN)
    progress = load_cycle(layout, manifest.cycle_id)[1]
    write_model(
        layout.progress_file(manifest.cycle_id),
        progress.model_copy(update={"batches": (*progress.batches, ref)}),
    )
    final = open_database(":memory:")
    rebuild_database(layout, final, now=NOW, retention_days=365)
    counts = _counts(final)
    assert counts["attempts"] == 3
    assert _job_statuses(final, manifest.cycle_id)["completed"] == 3


def test_missing_scheduler_state(two_cycles):
    layout, *_ = two_cycles
    layout.scheduler_file.unlink()
    with pytest.raises(StateMissingError):
        rebuild_database(layout, open_database(":memory:"), now=NOW, retention_days=365)


def test_missing_state_dir(tmp_path):
    layout = DataLayout(tmp_path / "pusty")
    with pytest.raises(StateMissingError):
        check_schema_version(layout)
    with pytest.raises(StateMissingError):
        load_scheduler_state(layout)


def test_schema_version_mismatch(two_cycles):
    layout, *_ = two_cycles
    data = json.loads(layout.schema_version_file.read_text())
    data["schema_version"] = 99
    layout.schema_version_file.write_text(json.dumps(data))
    with pytest.raises(SchemaMismatchError):
        rebuild_database(layout, open_database(":memory:"), now=NOW, retention_days=365)


def test_event_schema_version_mismatch(two_cycles):
    layout, *_ = two_cycles
    data = json.loads(layout.schema_version_file.read_text())
    data["event_schema_version"] = 2
    layout.schema_version_file.write_text(json.dumps(data))
    with pytest.raises(SchemaMismatchError):
        check_schema_version(layout)


def test_manifest_with_wrong_plan_digest(two_cycles):
    layout, first, _ = two_cycles
    write_model(layout.manifest_file(first.cycle_id), first.model_copy(update={"plan_digest": "x"}))
    with pytest.raises(RestoreError, match="nie zgadza się"):
        rebuild_database(layout, open_database(":memory:"), now=NOW, retention_days=365)


def test_corrupted_batch_stops_restore(two_cycles):
    layout, first, _ = two_cycles
    progress = load_cycle(layout, first.cycle_id)[1]
    path = layout.cycle_dir(first.cycle_id) / progress.batches[1].path
    path.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(RestoreError, match="uszkodzony"):
        rebuild_database(layout, open_database(":memory:"), now=NOW, retention_days=365)


def test_progress_of_other_cycle_rejected(two_cycles):
    layout, first, second = two_cycles
    shutil.copy(layout.progress_file(second.cycle_id), layout.progress_file(first.cycle_id))
    with pytest.raises(RestoreError, match="innego cyklu"):
        load_cycle(layout, first.cycle_id)


def test_missing_progress_file(two_cycles):
    layout, first, _ = two_cycles
    layout.progress_file(first.cycle_id).unlink()
    with pytest.raises(RestoreError):
        load_cycle(layout, first.cycle_id)


def test_duplicate_attempt_across_batches_is_restore_error(tmp_path):
    layout = init_layout(tmp_path)
    manifest = make_manifest()
    event = attempt_event(manifest, planned_for(manifest)[0])
    write_cycle(layout, manifest, [[event], [event]])
    with pytest.raises(RestoreError, match="niespójne"):
        rebuild_database(layout, open_database(":memory:"), now=NOW, retention_days=365)


def test_initialize_data_dir(tmp_path):
    layout = init_layout(tmp_path)
    state = load_scheduler_state(layout)
    assert state.last_started_slot is None and state.active_cycle_id is None
    assert state.interval_hours == 72
    assert check_schema_version(layout).initialized_at == T0
    assert (layout.cycles_dir / ".gitkeep").exists()
    assert (layout.reports_dir / ".gitkeep").exists()
    with pytest.raises(FileExistsError):
        initialize_data_dir(layout, make_config().settings, T0)


def test_progress_model_roundtrip_after_restore(two_cycles):
    layout, first, _ = two_cycles
    manifest, progress = load_cycle(layout, first.cycle_id)
    assert manifest == first
    assert isinstance(progress, CycleProgress) and len(progress.batches) == 3

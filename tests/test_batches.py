from __future__ import annotations

import gzip
import re

import pytest

from booking_monitor.models import JobStatus
from booking_monitor.storage.batches import (
    BatchIntegrityError,
    new_batch_id,
    read_batch,
    serialize_events,
    sha256_hex,
    write_batch,
)
from booking_monitor.storage.layout import DataLayout
from tests.storage_helpers import RUN, T0, attempt_event, make_manifest, planned_for


@pytest.fixture
def events():
    manifest = make_manifest()
    planned = planned_for(manifest)
    return manifest, [
        attempt_event(manifest, planned[0]),
        attempt_event(manifest, planned[1], status=JobStatus.RETRY_WAIT),
        attempt_event(manifest, planned[2], offers=0),
    ]


def test_new_batch_id_format():
    batch_id = new_batch_id(T0)
    assert re.fullmatch(r"b20261104T051700Z-[0-9a-f]{8}", batch_id)
    assert new_batch_id(T0) != batch_id


def test_roundtrip(tmp_path, events):
    manifest, items = events
    layout = DataLayout(tmp_path)
    ref = write_batch(layout, manifest.cycle_id, "b1", items, created_at=T0, run=RUN)
    assert ref.path == "batches/b1.jsonl.gz"
    assert ref.events == 3
    assert ref.sha256 == sha256_hex(serialize_events(items))
    assert read_batch(layout, manifest.cycle_id, ref) == items


def test_bytes_are_deterministic(tmp_path, events):
    manifest, items = events
    a = DataLayout(tmp_path / "a")
    b = DataLayout(tmp_path / "b")
    write_batch(a, manifest.cycle_id, "b1", items, created_at=T0, run=RUN)
    write_batch(b, manifest.cycle_id, "b1", items, created_at=T0, run=RUN)
    data = a.batch_file(manifest.cycle_id, "b1").read_bytes()
    assert data == b.batch_file(manifest.cycle_id, "b1").read_bytes()
    assert serialize_events(items).endswith(b"\n")
    assert gzip.decompress(data) == serialize_events(items)


def test_rewrite_same_content_is_idempotent_other_content_rejected(tmp_path, events):
    manifest, items = events
    layout = DataLayout(tmp_path)
    first = write_batch(layout, manifest.cycle_id, "b1", items, created_at=T0, run=RUN)
    again = write_batch(layout, manifest.cycle_id, "b1", items, created_at=T0, run=RUN)
    assert again == first
    with pytest.raises(BatchIntegrityError):
        write_batch(layout, manifest.cycle_id, "b1", items[:1], created_at=T0, run=RUN)


def test_checksum_mismatch_detected(tmp_path, events):
    manifest, items = events
    layout = DataLayout(tmp_path)
    ref = write_batch(layout, manifest.cycle_id, "b1", items, created_at=T0, run=RUN)
    path = layout.batch_file(manifest.cycle_id, "b1")
    tampered = gzip.decompress(path.read_bytes()).replace(b"64000", b"64001")
    path.write_bytes(gzip.compress(tampered))
    with pytest.raises(BatchIntegrityError, match="suma kontrolna"):
        read_batch(layout, manifest.cycle_id, ref)


def test_event_count_mismatch_detected(tmp_path, events):
    manifest, items = events
    layout = DataLayout(tmp_path)
    ref = write_batch(layout, manifest.cycle_id, "b1", items, created_at=T0, run=RUN)
    with pytest.raises(BatchIntegrityError, match="oczekiwano"):
        read_batch(layout, manifest.cycle_id, ref.model_copy(update={"events": 4}))


def test_truncated_gzip_detected(tmp_path, events):
    manifest, items = events
    layout = DataLayout(tmp_path)
    ref = write_batch(layout, manifest.cycle_id, "b1", items, created_at=T0, run=RUN)
    path = layout.batch_file(manifest.cycle_id, "b1")
    path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(BatchIntegrityError, match="gzip"):
        read_batch(layout, manifest.cycle_id, ref)


def test_missing_file_detected(tmp_path, events):
    manifest, items = events
    layout = DataLayout(tmp_path)
    ref = write_batch(layout, manifest.cycle_id, "b1", items, created_at=T0, run=RUN)
    layout.batch_file(manifest.cycle_id, "b1").unlink()
    with pytest.raises(BatchIntegrityError, match="brak pliku"):
        read_batch(layout, manifest.cycle_id, ref)


def test_event_from_other_cycle_rejected(tmp_path, events):
    _manifest, items = events
    layout = DataLayout(tmp_path)
    ref = write_batch(layout, "c-inny", "b1", items, created_at=T0, run=RUN)
    with pytest.raises(BatchIntegrityError, match="zdarzenie cyklu"):
        read_batch(layout, "c-inny", ref)

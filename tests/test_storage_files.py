from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from booking_monitor.storage.files import read_model, write_model
from booking_monitor.storage.state import SchedulerState


def _state() -> SchedulerState:
    return SchedulerState(
        anchor_at_utc=datetime(2026, 11, 4, 5, tzinfo=UTC),
        interval_hours=72,
        last_started_slot=3,
        active_cycle_id="c20261113-s0003",
        updated_at=datetime(2026, 11, 13, 5, 17, tzinfo=UTC),
    )


def test_write_and_read_roundtrip(tmp_path):
    path = tmp_path / "nested" / "state" / "scheduler.json"
    write_model(path, _state())
    assert read_model(path, SchedulerState) == _state()


def test_format_is_stable_and_sorted(tmp_path):
    path = tmp_path / "s.json"
    write_model(path, _state())
    text = path.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    keys = list(json.loads(text).keys())
    assert keys == sorted(keys)
    assert '\n  "active_cycle_id"' in text
    first = path.read_bytes()
    write_model(path, _state())
    assert path.read_bytes() == first


def test_no_temporary_files_left(tmp_path):
    write_model(tmp_path / "a.json", _state())
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_model(tmp_path / "brak.json", SchedulerState)

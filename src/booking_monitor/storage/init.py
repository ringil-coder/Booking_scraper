"""Jawna inicjalizacja katalogu danych (A§6: pierwsza inicjalizacja to osobny krok)."""

from __future__ import annotations

from datetime import datetime

from booking_monitor.config import Settings
from booking_monitor.storage.events import EVENT_SCHEMA_VERSION
from booking_monitor.storage.files import write_model, write_text_atomic
from booking_monitor.storage.layout import DataLayout
from booking_monitor.storage.state import SchedulerState, SchemaVersionFile


def initialize_data_dir(layout: DataLayout, settings: Settings, now: datetime) -> None:
    """Tworzy początkowy stan; odmawia, jeśli stan już istnieje."""
    if now.tzinfo is None:
        raise ValueError("oczekiwano czasu ze strefą (UTC)")
    if layout.scheduler_file.exists() or layout.schema_version_file.exists():
        raise FileExistsError(f"stan już istnieje w {layout.state_dir} — inicjalizacja odrzucona")
    if layout.cycle_ids():
        raise FileExistsError(f"katalog {layout.cycles_dir} zawiera już cykle")
    write_model(
        layout.schema_version_file,
        SchemaVersionFile(event_schema_version=EVENT_SCHEMA_VERSION, initialized_at=now),
    )
    write_model(
        layout.scheduler_file,
        SchedulerState(
            anchor_at_utc=settings.schedule.anchor_at_utc,
            interval_hours=settings.schedule.interval_hours,
            updated_at=now,
        ),
    )
    for directory in (layout.cycles_dir, layout.reports_dir):
        write_text_atomic(directory / ".gitkeep", "")

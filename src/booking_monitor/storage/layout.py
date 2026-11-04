"""Układ katalogów gałęzi `monitor-data` (A§8)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataLayout:
    root: Path

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def scheduler_file(self) -> Path:
        return self.state_dir / "scheduler.json"

    @property
    def schema_version_file(self) -> Path:
        return self.state_dir / "schema-version.json"

    @property
    def cycles_dir(self) -> Path:
        return self.root / "cycles"

    def cycle_dir(self, cycle_id: str) -> Path:
        return self.cycles_dir / cycle_id

    def manifest_file(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "manifest.json"

    def progress_file(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "progress.json"

    def batches_dir(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "batches"

    def batch_file(self, cycle_id: str, batch_id: str) -> Path:
        return self.batches_dir(cycle_id) / f"{batch_id}.jsonl.gz"

    @staticmethod
    def batch_relpath(batch_id: str) -> str:
        return f"batches/{batch_id}.jsonl.gz"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def latest_report_file(self) -> Path:
        return self.reports_dir / "latest.json"

    def cycle_ids(self) -> list[str]:
        if not self.cycles_dir.is_dir():
            return []
        return sorted(p.name for p in self.cycles_dir.iterdir() if p.is_dir())

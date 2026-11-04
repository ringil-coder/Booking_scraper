"""Paczka raportu publikowana jako artefakt workflow (A§8, A§11).

Pliki: `report.html`, `prices.csv`, `summary.md`, `manifest.json` i opcjonalna
spójna kopia bazy `monitor.sqlite3` (przez mechanizm backupu SQLite).
`LatestReport` to krótki wpis zapisywany przez krok finalize w `reports/latest.json`.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from booking_monitor.analytics import CycleReport, ReportModel, build_cycle_report
from booking_monitor.reporting.csv_export import write_prices_csv
from booking_monitor.reporting.html import write_html
from booking_monitor.reporting.summary import render_summary_markdown
from booking_monitor.storage.events import RunContext

REPORT_SCHEMA_VERSION = 1
REPORT_HTML = "report.html"
PRICES_CSV = "prices.csv"
SUMMARY_MD = "summary.md"
MANIFEST_JSON = "manifest.json"
SQLITE_COPY = "monitor.sqlite3"


class ReportFile(ReportModel):
    name: str
    sha256: str
    bytes: int


class ReportManifest(ReportModel):
    schema_version: int = REPORT_SCHEMA_VERSION
    cycle_id: str
    generated_at: datetime
    status: str
    partial_quality: bool
    coverage_pct: float
    jobs_total: int
    job_counts: dict[str, int]
    comparable_observations: int
    oldest_observed_at: datetime | None = None
    newest_observed_at: datetime | None = None
    parser_versions: tuple[str, ...]
    code_sha: str | None = None
    config_hash: str
    run: RunContext
    files: tuple[ReportFile, ...]


class LatestReport(ReportModel):
    """Wpis `reports/latest.json` na gałęzi danych — wskazuje ostatni raport."""

    schema_version: int = REPORT_SCHEMA_VERSION
    cycle_id: str
    generated_at: datetime
    status: str
    partial_quality: bool
    coverage_pct: float
    jobs_total: int
    job_counts: dict[str, int]
    oldest_observed_at: datetime | None = None
    newest_observed_at: datetime | None = None
    run: RunContext
    files: tuple[str, ...]


def _file_entry(path: Path) -> ReportFile:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ReportFile(name=path.name, sha256=digest, bytes=path.stat().st_size)


def backup_database(conn: sqlite3.Connection, destination: Path) -> Path:
    """Spójna kopia bazy przez SQLite Online Backup (nie kopiujemy otwartego pliku)."""
    if destination.exists():
        destination.unlink()
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
    finally:
        target.close()
    return destination


def build_report_bundle(
    conn: sqlite3.Connection,
    cycle_id: str,
    out_dir: Path,
    *,
    now: datetime,
    tz: ZoneInfo,
    run: RunContext,
    include_sqlite: bool = True,
    artifact_name: str | None = None,
    report: CycleReport | None = None,
) -> ReportManifest:
    """Buduje raport cyklu i zapisuje pliki artefaktu w `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    report = report or build_cycle_report(conn, cycle_id, now=now, tz=tz)
    written = [
        write_html(report, out_dir / REPORT_HTML),
        write_prices_csv(report, out_dir / PRICES_CSV),
    ]
    summary_path = out_dir / SUMMARY_MD
    summary_path.write_text(
        render_summary_markdown(report, artifact_name=artifact_name), encoding="utf-8"
    )
    written.append(summary_path)
    if include_sqlite:
        written.append(backup_database(conn, out_dir / SQLITE_COPY))

    ov = report.overview
    manifest = ReportManifest(
        cycle_id=ov.cycle_id,
        generated_at=report.generated_at,
        status=ov.status,
        partial_quality=ov.partial_quality,
        coverage_pct=ov.coverage_pct,
        jobs_total=ov.jobs_total,
        job_counts=ov.job_counts,
        comparable_observations=ov.comparable_observations,
        oldest_observed_at=ov.oldest_observed_at,
        newest_observed_at=ov.newest_observed_at,
        parser_versions=ov.parser_versions,
        code_sha=ov.code_sha,
        config_hash=ov.config_hash,
        run=run,
        files=tuple(_file_entry(path) for path in written),
    )
    (out_dir / MANIFEST_JSON).write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def latest_report_entry(manifest: ReportManifest) -> LatestReport:
    return LatestReport(
        cycle_id=manifest.cycle_id,
        generated_at=manifest.generated_at,
        status=manifest.status,
        partial_quality=manifest.partial_quality,
        coverage_pct=manifest.coverage_pct,
        jobs_total=manifest.jobs_total,
        job_counts=manifest.job_counts,
        oldest_observed_at=manifest.oldest_observed_at,
        newest_observed_at=manifest.newest_observed_at,
        run=manifest.run,
        files=(*(f.name for f in manifest.files), MANIFEST_JSON),
    )

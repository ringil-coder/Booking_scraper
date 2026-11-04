"""Podsumowanie cyklu w Markdown dla `$GITHUB_STEP_SUMMARY` (A§11)."""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from booking_monitor.analytics import (
    CYCLE_STATUS_LABELS,
    MODE_LOWEST,
    TECHNICAL_REASON_LABELS,
    CycleReport,
)

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|#])")


def md_escape(text: str) -> str:
    """Tekst bezpieczny w komórce tabeli Markdown."""
    return _MD_SPECIAL.sub(r"\\\1", text.replace("\r", " ").replace("\n", " "))


def _local(moment: datetime | None, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%d.%m.%Y %H:%M") if moment else "—"


def _day(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def _pct(value: float) -> str:
    return f"{value:.1f}".replace(".", ",") + "%"


def render_summary_markdown(report: CycleReport, *, artifact_name: str | None = None) -> str:
    tz = ZoneInfo(report.timezone)
    ov = report.overview
    ds = report.data_state
    status = CYCLE_STATUS_LABELS.get(ov.status, ov.status)
    if ov.status == "blocked":
        status_line = f"⛔ **{status}** — raport niepełny"
    elif ov.partial_quality:
        status_line = f"⚠️ **{status}** — raport częściowy (niepełny jakościowo)"
    else:
        status_line = f"✅ **{status}**"
    last_checkin = date.fromordinal(ov.end_date_exclusive.toordinal() - 1)

    lines = [
        f"## Monitor konkurencji — cykl `{ov.cycle_id}`",
        "",
        f"Status: {status_line}",
        "",
        "| | |",
        "|---|---|",
        f"| Apartament | {md_escape(ov.apartment_name)} |",
        f"| Przyjazdy | {_day(ov.start_date)} – {_day(last_checkin)} · "
        f"{'/'.join(str(n) for n in ov.stay_nights)} noce |",
        f"| Czas odczytów | {_local(ov.oldest_observed_at, tz)} – "
        f"{_local(ov.newest_observed_at, tz)} ({report.timezone}) |",
        f"| Pokrycie | {_pct(ov.coverage_pct)} ({ds.executed} z {ds.jobs_total} zapytań) |",
        f"| Porównywalne ceny | {ov.comparable_observations} z {ov.observations_total} "
        f"odczytanych stron · {ov.competitors_total} konkurentów |",
        f"| Brakujące / wygasłe / błędy | {ds.missing} / {ds.expired} / {ds.failed} |",
        f"| Artefakt | {md_escape(artifact_name) if artifact_name else '—'} |",
        "",
    ]
    if ds.failed_by_reason:
        lines += ["### Błędy wg przyczyny", ""]
        for reason, count in ds.failed_by_reason.items():
            lines.append(f"- {TECHNICAL_REASON_LABELS.get(reason, md_escape(reason))}: {count}")
        lines.append("")

    warnings = []
    if ov.status == "blocked":
        detail = f": {md_escape(ov.blocked_message)}" if ov.blocked_message else ""
        warnings.append(
            f"⛔ Cykl zablokowany (blokada lub CAPTCHA){detail}. Wznowienie wymaga ręcznej decyzji."
        )
    if ov.partial_quality:
        warnings.append(
            f"⚠️ Raport częściowy — status cyklu „{status}”; brakuje "
            f"{ds.missing + ds.expired + ds.failed} z {ds.jobs_total} pomiarów."
        )
    lowest_cells = [c for c in report.cells(MODE_LOWEST) if c.n_comparable > 0]
    small = [c for c in lowest_cells if c.small_sample]
    if small:
        warnings.append(
            f"Mała próba (mniej niż {report.small_sample_threshold} porównywalnych cen) "
            f"w {len(small)} z {len(lowest_cells)} terminów z cenami."
        )
    if warnings:
        lines += ["### Ostrzeżenia", ""] + [f"- {w}" for w in warnings] + [""]
    lines.append(
        "_Ceny pochodzą z zapisanych odczytów; czas każdego odczytu jest w raporcie i w CSV._"
    )
    return "\n".join(lines) + "\n"

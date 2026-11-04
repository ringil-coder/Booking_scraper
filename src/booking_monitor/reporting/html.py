"""Samodzielny raport HTML (A§11).

Jeden plik: CSS i JS wstawione inline, dane osadzone jako JSON. Brak zewnętrznych
żądań (CDN, czcionek, obrazów) — raport działa po otwarciu z dysku. Odnośniki
`<a href>` do stron ofert są tworzone z danych i otwierają się dopiero po kliknięciu.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from booking_monitor.analytics import (
    AVAILABILITY_LABELS,
    CYCLE_STATUS_LABELS,
    JOB_STATUS_LABELS,
    MEAL_PLAN_LABELS,
    NON_COMPARABLE_LABELS,
    PREPAYMENT_LABELS,
    RATE_CATEGORY_LABELS,
    RATE_MODE_LABELS,
    RATE_MODES,
    TECHNICAL_REASON_LABELS,
    CycleReport,
    format_money,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _local(moment: datetime | None, tz: ZoneInfo) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def _pl_date(day: date | None) -> str:
    return day.strftime("%d.%m.%Y") if day else "—"


def plural_pl(n: int, one: str, few: str, many: str) -> str:
    """Odmiana liczebnika: 1 błąd, 2–4 błędy, 5+ błędów (z wyjątkiem 12–14)."""
    if n == 1:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def embed_json(data: object) -> str:
    """JSON bezpieczny wewnątrz `<script type="application/json">`."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def build_view_data(report: CycleReport) -> dict:
    """Zwarta postać danych dla skryptu raportu (indeksy zamiast powtórzeń tekstu)."""
    tz = ZoneInfo(report.timezone)
    ov = report.overview
    competitor_index = {c.competitor_id: i for i, c in enumerate(report.competitors)}
    days = (ov.end_date_exclusive - ov.start_date).days
    dates = [date.fromordinal(ov.start_date.toordinal() + i) for i in range(days)]
    date_index = {d: i for i, d in enumerate(dates)}
    cycle_index = {cid: i for i, cid in enumerate(report.history_cycles)}
    units: dict[str, int] = {}

    def unit(name: str) -> int:
        return units.setdefault(name, len(units))

    results = []
    result_index: dict[str, int] = {}
    for r in report.results:
        if r.checkin not in date_index:
            continue
        result_index[r.query_id] = len(results)
        offers = [
            [
                o.rate_category,
                o.meal_plan,
                o.prepayment,
                o.total_price_grosze,
                o.avg_night_price_grosze,
                int(o.selected_for_category),
                int(o.comparable),
                unit(o.unit_name),
                _pl_date(o.free_cancellation_until) if o.free_cancellation_until else None,
                o.price_completeness,
                o.displayed_price_grosze,
                o.offer_index,
                o.currency,
            ]
            for o in r.offers
        ]
        stale = None
        if r.stale is not None:
            stale = [
                cycle_index.get(r.stale.cycle_id, -1),
                r.stale.total_price_grosze,
                r.stale.avg_night_price_grosze,
                _local(r.stale.observed_at, tz),
                r.stale.rate_category,
                unit(r.stale.unit_name),
                r.stale.cycle_id,
            ]
        results.append(
            {
                "c": competitor_index[r.competitor_id],
                "d": date_index[r.checkin],
                "n": r.nights,
                "a": r.availability,
                "s": r.job_status,
                "r": r.technical_reason,
                "m": r.message,
                "t": _local(r.observed_at, tz),
                "x": r.non_comparable_reason,
                "o": offers,
                "st": stale,
            }
        )

    cells = {
        mode: [
            [
                date_index[c.checkin],
                c.nights,
                c.n_comparable,
                c.min_total_grosze,
                c.median_total_grosze,
                c.min_avg_night_grosze,
                c.median_avg_night_grosze,
            ]
            for c in report.cells(mode)
            if c.checkin in date_index
        ]
        for mode in RATE_MODES
    }

    history = [
        [
            result_index[h.query_id],
            [
                [
                    cycle_index[p.cycle_id],
                    p.total_price_grosze,
                    p.availability,
                    _local(p.observed_at, tz),
                ]
                for p in h.points
            ],
            h.change_vs_previous_grosze,
            h.change_vs_previous_pct,
        ]
        for h in report.history
        if h.query_id in result_index
    ]

    return {
        "meta": {
            "cycleId": ov.cycle_id,
            "currency": ov.currency,
            "nights": list(ov.stay_nights),
            "threshold": report.small_sample_threshold,
            "modes": [[m, RATE_MODE_LABELS[m]] for m in RATE_MODES],
            "labels": {
                "availability": AVAILABILITY_LABELS,
                "job": JOB_STATUS_LABELS,
                "reason": TECHNICAL_REASON_LABELS,
                "category": RATE_CATEGORY_LABELS,
                "meal": MEAL_PLAN_LABELS,
                "prepayment": PREPAYMENT_LABELS,
                "nonComparable": NON_COMPARABLE_LABELS,
            },
        },
        "competitors": [[c.competitor_id, c.name, c.canonical_url] for c in report.competitors],
        "dates": [d.isoformat() for d in dates],
        "units": list(units),
        "cycles": list(report.history_cycles),
        "results": results,
        "cells": cells,
        "history": history,
    }


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = format_money
    env.filters["pldate"] = _pl_date
    env.filters["plural"] = plural_pl
    return env


def render_html(report: CycleReport) -> str:
    """Renderuje raport cyklu do jednego, samodzielnego pliku HTML."""
    tz = ZoneInfo(report.timezone)
    ov = report.overview
    env = _environment()
    template = env.get_template("report.html.j2")
    competitor_names = {c.competitor_id: c.name for c in report.competitors}
    return template.render(
        report=report,
        ov=ov,
        ds=report.data_state,
        local=lambda m: _local(m, tz) or "—",
        status_label=CYCLE_STATUS_LABELS.get(ov.status, ov.status),
        last_checkin=date.fromordinal(ov.end_date_exclusive.toordinal() - 1),
        coverage=f"{ov.coverage_pct:.1f}".replace(".", ","),
        job_labels=JOB_STATUS_LABELS,
        reason_labels=TECHNICAL_REASON_LABELS,
        competitor_names=competitor_names,
        css=(TEMPLATES_DIR / "report.css").read_text(encoding="utf-8"),
        script=(TEMPLATES_DIR / "report.js").read_text(encoding="utf-8"),
        data_json=embed_json(build_view_data(report)),
    )


def write_html(report: CycleReport, path: Path) -> Path:
    path.write_text(render_html(report), encoding="utf-8")
    return path

"""Eksport CSV dla Excela (A§11).

UTF-8 z BOM, separator `;`, daty ISO, kwoty z przecinkiem dziesiętnym. Komórki
tekstowe zaczynające się od `= + - @`, tabulatora lub CR poprzedzamy apostrofem,
żeby arkusz nie zinterpretował ich jako formuły.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from booking_monitor.analytics import (
    MODE_LOWEST,
    NON_COMPARABLE_LABELS,
    TECHNICAL_REASON_LABELS,
    CycleReport,
    comparable_offer,
)

COLUMNS = (
    "cycle_id",
    "price_cycle_id",
    "competitor_id",
    "competitor_name",
    "checkin",
    "checkout",
    "nights",
    "job_status",
    "availability",
    "technical_reason",
    "rate_category",
    "unit_name",
    "total_price",
    "avg_night_price",
    "displayed_price",
    "currency",
    "price_completeness",
    "comparable",
    "is_lowest",
    "meal_plan",
    "prepayment",
    "free_cancellation_until",
    "observed_at",
    "stale",
    "note",
)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def protect_cell(value: object) -> str:
    """Tekst bezpieczny dla arkusza (ochrona przed wstrzyknięciem formuły)."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def amount(grosze: int | None) -> str:
    """Grosze → „1234,56” (przecinek dziesiętny, bez separatora tysięcy)."""
    if grosze is None:
        return ""
    sign = "-" if grosze < 0 else ""
    whole, cents = divmod(abs(grosze), 100)
    return f"{sign}{whole},{cents:02d}"


def iso_moment(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def iso_day(day: date | None) -> str:
    return day.isoformat() if day else ""


def _flag(value: bool) -> str:
    return "true" if value else "false"


def iter_rows(report: CycleReport) -> Iterator[dict[str, str]]:
    """Wiersze CSV: jedna oferta wybrana w kategorii na wiersz; bez ofert — jeden wiersz."""
    cycle_id = report.overview.cycle_id
    names = {c.competitor_id: c.name for c in report.competitors}
    for r in report.results:
        lowest = comparable_offer(r.offers, MODE_LOWEST) if r.availability == "available" else None
        notes = []
        if r.non_comparable_reason:
            notes.append(
                NON_COMPARABLE_LABELS.get(r.non_comparable_reason, r.non_comparable_reason)
            )
        if r.technical_reason:
            notes.append(TECHNICAL_REASON_LABELS.get(r.technical_reason, r.technical_reason))
        if r.message:
            notes.append(r.message)
        base = {
            "cycle_id": cycle_id,
            "price_cycle_id": cycle_id,
            "competitor_id": r.competitor_id,
            "competitor_name": names.get(r.competitor_id, r.competitor_id),
            "checkin": iso_day(r.checkin),
            "checkout": iso_day(r.checkout),
            "nights": str(r.nights),
            "job_status": r.job_status,
            "availability": r.availability,
            "technical_reason": r.technical_reason or "",
            "observed_at": iso_moment(r.observed_at),
            "stale": "false",
            "note": " · ".join(notes),
        }
        selected = [o for o in r.offers if o.selected_for_category]
        if selected:
            for offer in selected:
                yield {
                    **base,
                    "rate_category": offer.rate_category,
                    "unit_name": offer.unit_name,
                    "total_price": amount(offer.total_price_grosze),
                    "avg_night_price": amount(offer.avg_night_price_grosze),
                    "displayed_price": amount(offer.displayed_price_grosze),
                    "currency": offer.currency,
                    "price_completeness": offer.price_completeness,
                    "comparable": _flag(offer.comparable and r.availability == "available"),
                    "is_lowest": _flag(lowest is not None and offer == lowest),
                    "meal_plan": offer.meal_plan,
                    "prepayment": offer.prepayment,
                    "free_cancellation_until": iso_day(offer.free_cancellation_until),
                }
        elif r.stale is not None:
            stale = r.stale
            yield {
                **base,
                "price_cycle_id": stale.cycle_id,
                "rate_category": stale.rate_category,
                "unit_name": stale.unit_name,
                "total_price": amount(stale.total_price_grosze),
                "avg_night_price": amount(stale.avg_night_price_grosze),
                "displayed_price": "",
                "currency": stale.currency,
                "price_completeness": "complete",
                "comparable": "false",
                "is_lowest": "false",
                "meal_plan": "",
                "prepayment": "",
                "free_cancellation_until": "",
                "observed_at": iso_moment(stale.observed_at),
                "stale": "true",
                "note": " · ".join(
                    [f"Cena nieaktualna z cyklu {stale.cycle_id}; poza statystykami", *notes]
                ),
            }
        else:
            yield {
                **base,
                "rate_category": "",
                "unit_name": "",
                "total_price": "",
                "avg_night_price": "",
                "displayed_price": "",
                "currency": "",
                "price_completeness": "",
                "comparable": "false",
                "is_lowest": "false",
                "meal_plan": "",
                "prepayment": "",
                "free_cancellation_until": "",
            }


def write_prices_csv(report: CycleReport, path: Path) -> Path:
    """Zapisuje `prices.csv` (UTF-8 z BOM, separator `;`)."""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", lineterminator="\r\n")
        writer.writerow(COLUMNS)
        for row in iter_rows(report):
            writer.writerow([protect_cell(row[column]) for column in COLUMNS])
    return path

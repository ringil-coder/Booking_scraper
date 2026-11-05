"""Pilotaż E1: kilka odczytów Booking.com i pomiar czasu, jakości oraz blokad.

Narzędzie jednorazowe (PLAN_DZIALANIA.md, E1) — nie jest częścią cyklu produkcyjnego
i nie może być uruchamiane w CI. Zapisuje dla każdego odczytu HTML, zrzut ekranu
i linię JSON, a na końcu `summary.json` i szablon ręcznej kontroli cen.

Przykład:

    uv run python spike/probe.py --competitor all --sample 4 --nights 2 3 4 --out spike/output
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from booking_monitor.config import AppConfig, ConfigError, Profile, load_config
from booking_monitor.dates import CycleWindow, local_date
from booking_monitor.models import PageContext, ParsedPage, StayQuery, TechnicalReason
from booking_monitor.parsing import PARSER_VERSION, parse_property_page
from booking_monitor.providers.base import FetchError
from booking_monitor.providers.booking_browser import BookingBrowserProvider
from booking_monitor.redaction import scrub_html
from booking_monitor.storage.state import CompetitorSnapshot

EXIT_BLOCKED = 3
JOB_OVERHEAD_MINUTES = 5  # instalacja zależności i przeglądarki na job (PLAN §3: 3–5 min)


# --------------------------------------------------------------------------- wybór zapytań


def sample_dates(window: CycleWindow, count: int) -> list[date]:
    """`count` dat przyjazdu równomiernie rozłożonych na horyzoncie (z pierwszą i ostatnią)."""
    dates = window.checkin_dates()
    if count >= len(dates):
        return dates
    if count <= 1:
        return dates[:1]
    picked = [dates[round(i * (len(dates) - 1) / (count - 1))] for i in range(count)]
    return list(dict.fromkeys(picked))


def select_competitors(config: AppConfig, keys: list[str]) -> list[CompetitorSnapshot]:
    chosen = config.competitors if "all" in keys else [config.competitor(k) for k in keys]
    return [
        CompetitorSnapshot(
            competitor_id=c.competitor_id,
            name=c.name,
            booking_url=c.booking_url,
            canonical_url=c.canonical_url,
            unit_filter=c.unit_filter,
        )
        for c in chosen
    ]


def build_queries(
    competitors: list[CompetitorSnapshot], dates: list[date], nights: list[int], profile_hash: str
) -> list[tuple[CompetitorSnapshot, StayQuery]]:
    """Kolejność jak w planie cyklu: data, długość pobytu, przeplot konkurentów."""
    return [
        (competitor, StayQuery.build(competitor.competitor_id, checkin, n, profile_hash))
        for checkin in sorted(dates)
        for n in sorted(nights)
        for competitor in competitors
    ]


# --------------------------------------------------------------------------- ocena odczytu


def context_mismatches(context: PageContext, query: StayQuery, profile: Profile) -> list[str]:
    """Parametry niepotwierdzone na stronie lub różne od żądanych (A§10 pkt 2)."""
    expected = {
        "checkin": query.checkin,
        "checkout": query.checkout,
        "adults": profile.adults,
        "children": len(profile.children_ages),
        "rooms": profile.units,
        "currency": profile.currency,
    }
    problems = []
    for field, wanted in expected.items():
        seen = getattr(context, field)
        if seen != wanted:
            problems.append(f"{field}: oczekiwano {wanted}, strona: {seen}")
    return problems


def summarize_offers(parsed: ParsedPage) -> list[dict]:
    return [
        {
            "unit_name": o.unit_name,
            "is_entire_apartment": o.is_entire_apartment,
            "max_occupancy": o.max_occupancy,
            "rate_category": o.rate_category.value,
            "meal_plan": o.meal_plan.value,
            "prepayment": o.prepayment.value,
            "free_cancellation_until": (
                o.free_cancellation_until.isoformat() if o.free_cancellation_until else None
            ),
            "currency": o.currency,
            "displayed_price": o.displayed_price_grosze / 100,
            "original_price": (o.original_price_grosze or 0) / 100 or None,
            "components": [c.model_dump(mode="json") for c in o.components],
            "fees_fully_disclosed": o.fees_fully_disclosed,
        }
        for o in parsed.offers
    ]


def console_detail(record: dict) -> str:
    """Krótki opis odczytu do konsoli: czy strona ma daty, najniższa cena albo przyczyna błędu."""
    context = record.get("context")
    if context is None:
        return (record.get("message") or "")[:100]
    dates = "tak" if context.get("checkin") and context.get("checkout") else "NIE"
    parts = [f"daty na stronie: {dates}", f"waluta: {context.get('currency') or '?'}"]
    prices = [o["displayed_price"] for o in record.get("offers") or []]
    if prices:
        currency = record["offers"][0]["currency"]
        parts.append(f"ofert: {len(prices)}, od {min(prices):.2f} {currency}")
    elif record.get("message"):
        parts.append(record["message"][:80])
    return " | ".join(parts)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def build_summary(
    records: list[dict], config: AppConfig, window: CycleWindow, stopped_by_block: bool
) -> dict:
    settings = config.settings
    seconds = [r["seconds"] for r in records]
    interpreted = [r for r in records if r["outcome"] in {"available", "unavailable"}]
    confirmed = [r for r in interpreted if not r.get("context_mismatches")]
    errors: dict[str, int] = {}
    for r in records:
        if r.get("technical_reason"):
            errors[r["technical_reason"]] = errors.get(r["technical_reason"], 0) + 1

    median = statistics.median(seconds) if seconds else None
    p95 = percentile(seconds, 0.95)
    observations = len(config.competitors) * len(window) * len(settings.collection.stay_nights)
    cycles_30d = 30 * 24 / settings.schedule.interval_hours
    budget = settings.execution.collection_budget_minutes_per_job

    def minutes(per_observation: float | None) -> float | None:
        return None if per_observation is None else observations * per_observation / 60

    minutes_p95 = minutes(p95)
    jobs = math.ceil(minutes_p95 / budget) if minutes_p95 else 0
    overhead = jobs * JOB_OVERHEAD_MINUTES
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "parser_version": PARSER_VERSION,
        "stopped_by_block": stopped_by_block,
        "observations": len(records),
        "interpreted": len(interpreted),
        "interpreted_with_confirmed_context": len(confirmed),
        "success_rate": round(len(confirmed) / len(records), 4) if records else None,
        "available": sum(r["outcome"] == "available" for r in records),
        "unavailable": sum(r["outcome"] == "unavailable" for r in records),
        "technical_errors": errors,
        "blocked": errors.get(TechnicalReason.BLOCKED.value, 0),
        "context_mismatches": sum(bool(r.get("context_mismatches")) for r in records),
        "seconds_per_observation": {
            "median": median,
            "p95": p95,
            "max": max(seconds) if seconds else None,
            "note": "z odstępem między nawigacjami",
        },
        "estimate": {
            "competitors_configured": len(config.competitors),
            "checkin_dates": len(window),
            "stay_lengths": len(settings.collection.stay_nights),
            "observations_per_cycle": observations,
            "minutes_per_cycle_median": minutes(median),
            "minutes_per_cycle_p95": minutes_p95,
            "hours_per_cycle_p95": None if minutes_p95 is None else minutes_p95 / 60,
            "jobs_per_cycle_p95": jobs,
            "job_overhead_minutes_per_cycle": overhead,
            "cycles_per_30_days": cycles_30d,
            "minutes_per_30_days_median": (
                None if median is None else (minutes(median) + overhead) * cycles_30d
            ),
            "minutes_per_30_days_p95": (
                None if minutes_p95 is None else (minutes_p95 + overhead) * cycles_30d
            ),
            "cycle_fits_48h_p95": None if minutes_p95 is None else minutes_p95 / 60 <= 48,
        },
    }


def write_manual_check(records: list[dict], path: Path) -> None:
    """Szablon ręcznej kontroli ~20 cen (PLAN_DZIALANIA.md, E1)."""
    columns = [
        "nr",
        "konkurent",
        "checkin",
        "noce",
        "wynik",
        "lokal",
        "taryfa",
        "cena_odczytana",
        "waluta",
        "cena_wlasciciela",
        "warunki_wlasciciela",
        "zgodne_tak_nie",
        "uwagi",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(columns)
        for r in records:
            offers = r.get("offers") or []
            cheapest = min(offers, key=lambda o: o["displayed_price"], default=None)
            writer.writerow(
                [
                    r["nr"],
                    r["competitor_id"],
                    r["checkin"],
                    r["nights"],
                    r["outcome"],
                    cheapest["unit_name"] if cheapest else "",
                    cheapest["rate_category"] if cheapest else "",
                    f"{cheapest['displayed_price']:.2f}".replace(".", ",") if cheapest else "",
                    cheapest["currency"] if cheapest else "",
                    "",
                    "",
                    "",
                    "",
                ]
            )


# --------------------------------------------------------------------------- uruchomienie


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pilotaż E1: odczyty Booking.com z pomiarami")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument(
        "--competitor",
        action="append",
        default=None,
        help="klucz konkurenta (można powtarzać) albo 'all'",
    )
    dates = parser.add_mutually_exclusive_group()
    dates.add_argument("--dates", nargs="+", type=date.fromisoformat, help="daty przyjazdu ISO")
    dates.add_argument("--sample", type=int, default=4, help="liczba dat z horyzontu")
    parser.add_argument("--nights", nargs="+", type=int, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-observations", type=int, default=60, help="bezpiecznik")
    parser.add_argument("--headed", action="store_true", help="widoczne okno (lokalnie)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config_dir)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    settings = config.settings
    profile = settings.profile
    try:
        competitors = select_competitors(config, args.competitor or ["all"])
    except KeyError as exc:
        print(f"nieznany konkurent: {exc}", file=sys.stderr)
        return 2

    window = CycleWindow.for_cycle(
        local_date(datetime.now(UTC), settings.tz), settings.collection.horizon_months
    )
    checkins = args.dates or sample_dates(window, args.sample)
    nights = args.nights or list(settings.collection.stay_nights)
    queries = build_queries(competitors, checkins, nights, config.profile_hash)
    if len(queries) > args.max_observations:
        print(
            f"{len(queries)} odczytów przekracza --max-observations={args.max_observations}",
            file=sys.stderr,
        )
        return 2

    pages = args.out / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    stopped_by_block = False
    provider = BookingBrowserProvider(
        settings.execution, diagnostics_dir=args.out / "diagnostics", headless=not args.headed
    )
    with provider, (args.out / "observations.jsonl").open("w", encoding="utf-8") as log:
        for nr, (competitor, query) in enumerate(queries, start=1):
            started = time.monotonic()
            record: dict = {
                "nr": nr,
                "competitor_id": competitor.competitor_id,
                "checkin": query.checkin.isoformat(),
                "checkout": query.checkout.isoformat(),
                "nights": query.nights,
                "started_at": datetime.now(UTC).isoformat(),
            }
            try:
                snapshot = provider.fetch(competitor, query, profile)
            except FetchError as exc:
                record.update(
                    outcome="technical_error",
                    technical_reason=exc.reason.value,
                    message=exc.message,
                    retry_after_seconds=exc.retry_after_seconds,
                    diagnostics=list(exc.diagnostics),
                )
            else:
                stem = f"{nr:03d}_{competitor.competitor_id}_{query.checkin}_{query.nights}n"
                (pages / f"{stem}.html").write_text(scrub_html(snapshot.html), encoding="utf-8")
                provider.save_screenshot(pages / f"{stem}.png")
                parsed = parse_property_page(snapshot.html, url=snapshot.requested_url)
                reason = parsed.technical_reason
                record.update(
                    outcome=reason.value if reason else parsed.availability.value,
                    technical_reason=reason.value if reason else None,
                    availability=parsed.availability.value,
                    message=parsed.message,
                    http_status=snapshot.http_status,
                    url=snapshot.requested_url,
                    final_url=snapshot.final_url,
                    timings=snapshot.timings,
                    property_name=parsed.property_name,
                    booking_property_id=parsed.booking_property_id,
                    context=parsed.context.model_dump(mode="json"),
                    context_mismatches=context_mismatches(parsed.context, query, profile),
                    offers=summarize_offers(parsed),
                    html_file=f"pages/{stem}.html",
                    screenshot_file=f"pages/{stem}.png",
                )
            record["seconds"] = round(time.monotonic() - started, 3)
            records.append(record)
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
            log.flush()
            print(
                f"[{nr}/{len(queries)}] {competitor.competitor_id} {query.checkin} "
                f"{query.nights}n → {record['outcome']} ({record['seconds']:.1f} s) "
                f"| {console_detail(record)}",
                flush=True,
            )
            if record.get("technical_reason") == TechnicalReason.BLOCKED.value:
                stopped_by_block = True
                print("Zatrzymano: blokada lub CAPTCHA — nie kontynuujemy (A§10).", flush=True)
                break

    summary = build_summary(records, config, window, stopped_by_block)
    (args.out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_manual_check(records, args.out / "manual_check.csv")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return EXIT_BLOCKED if stopped_by_block else 0


if __name__ == "__main__":
    raise SystemExit(main())

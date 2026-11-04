"""Analiza wyników cyklu na potrzeby raportu (ARCHITEKTURA.md §3, §11).

Raport powstaje wyłącznie z zapisanych danych (roboczej bazy SQLite) — jego
zbudowanie ani otwarcie nie wykonuje żadnych odczytów Booking (A§4).

Definicje:
- *Porównywalna cena* (konkurent × zapytanie): z najnowszej obserwacji zapytania
  w BIEŻĄCYM cyklu o dostępności `available`; oferta pasuje do filtra lokalu,
  jest wybrana w swojej kategorii, ma kompletną cenę w walucie profilu.
- Jeden konkurent wnosi najwyżej jedną cenę do każdej statystyki: w trybie
  „najniższa cena” najtańszą porównywalną ofertę, w trybie kategorii — ofertę
  wybraną w tej kategorii.
- Ceny z wcześniejszych cykli pokazujemy z ich datą (`stale=True`); nigdy nie
  uzupełniają statystyk bieżącego cyklu.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from booking_monitor.models import (
    Availability,
    CycleStatus,
    JobStatus,
    MealPlan,
    Prepayment,
    PriceCompleteness,
    RateCategory,
)

SMALL_SAMPLE_THRESHOLD = 5
DEFAULT_HISTORY_CYCLES = 10

MODE_LOWEST = "lowest"
RATE_MODES: tuple[str, ...] = (
    MODE_LOWEST,
    RateCategory.NON_REFUNDABLE.value,
    RateCategory.FREE_CANCELLATION.value,
)

MISSING_JOB_STATUSES = frozenset(
    {JobStatus.PENDING.value, JobStatus.RETRY_WAIT.value, JobStatus.RUNNING.value}
)

# Każdy status poza `completed` oznacza raport niepełny jakościowo (A§11).
PARTIAL_QUALITY_STATUSES = frozenset(set(CycleStatus) - {CycleStatus.COMPLETED})

# --------------------------------------------------------------------------- etykiety

RATE_MODE_LABELS = {
    MODE_LOWEST: "Najniższa cena",
    RateCategory.NON_REFUNDABLE.value: "Bezzwrotna",
    RateCategory.FREE_CANCELLATION.value: "Bezpłatne anulowanie",
}
RATE_CATEGORY_LABELS = {
    RateCategory.NON_REFUNDABLE.value: "Bezzwrotna",
    RateCategory.FREE_CANCELLATION.value: "Bezpłatne anulowanie",
    RateCategory.OTHER.value: "Inna",
    RateCategory.UNKNOWN.value: "Nieznana",
}
AVAILABILITY_LABELS = {
    Availability.AVAILABLE.value: "Dostępny",
    Availability.UNAVAILABLE.value: "Niedostępny",
    Availability.NO_MATCHING_OFFER.value: "Brak pasującej oferty",
    Availability.UNKNOWN.value: "Nieznana",
}
JOB_STATUS_LABELS = {
    JobStatus.PENDING.value: "Oczekuje",
    JobStatus.RUNNING.value: "W trakcie",
    JobStatus.RETRY_WAIT.value: "Czeka na ponowienie",
    JobStatus.COMPLETED.value: "Wykonane",
    JobStatus.FAILED.value: "Błąd",
    JobStatus.EXPIRED.value: "Wygasłe",
}
CYCLE_STATUS_LABELS = {
    CycleStatus.PLANNED.value: "Zaplanowany",
    CycleStatus.RUNNING.value: "W trakcie",
    CycleStatus.PARTIAL.value: "Częściowy",
    CycleStatus.COMPLETED.value: "Ukończony",
    CycleStatus.COMPLETED_WITH_ERRORS.value: "Ukończony z błędami",
    CycleStatus.BLOCKED.value: "Zablokowany",
}
TECHNICAL_REASON_LABELS = {
    "timeout": "Przekroczony czas",
    "network_error": "Błąd sieci",
    "blocked": "Blokada / CAPTCHA",
    "parse_error": "Błąd parsera",
    "context_mismatch": "Niezgodne parametry strony",
}
MEAL_PLAN_LABELS = {
    MealPlan.ROOM_ONLY.value: "Bez wyżywienia",
    MealPlan.BREAKFAST.value: "Śniadanie",
    MealPlan.HALF_BOARD.value: "Dwa posiłki",
    MealPlan.FULL_BOARD.value: "Trzy posiłki",
    MealPlan.ALL_INCLUSIVE.value: "All inclusive",
    MealPlan.UNKNOWN.value: "Nieznane",
}
PREPAYMENT_LABELS = {
    Prepayment.NO_PREPAYMENT.value: "Bez przedpłaty",
    Prepayment.PREPAYMENT_REQUIRED.value: "Przedpłata",
    Prepayment.UNKNOWN.value: "Nieznana",
}
NON_COMPARABLE_LABELS = {
    "partial_price": "Niepełna cena (nieznane opłaty)",
    "no_matching_offer": "Brak pasującej oferty",
    "currency_mismatch": "Inna waluta",
}

NBSP = " "


def format_money(grosze: int | None, currency: str = "PLN") -> str:
    """Grosze → „1 234,56 zł” (separator tysięcy: twarda spacja)."""
    if grosze is None:
        return "—"
    sign = "-" if grosze < 0 else ""
    whole, cents = divmod(abs(grosze), 100)
    digits = f"{whole:,}".replace(",", NBSP)
    unit = "zł" if currency == "PLN" else currency
    return f"{sign}{digits},{cents:02d}{NBSP}{unit}"


def median_grosze(values: Sequence[int]) -> int | None:
    """Mediana w groszach; połówka grosza zaokrąglana w górę (ROUND_HALF_UP)."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    total = ordered[mid - 1] + ordered[mid]
    return (total + 1) // 2 if total >= 0 else -((-total) // 2)


# --------------------------------------------------------------------------- modele


class ReportModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CompetitorInfo(ReportModel):
    competitor_id: str
    name: str
    canonical_url: str
    unit_filter: str | None = None


class OfferView(ReportModel):
    """Oferta pasująca do filtra lokalu z najnowszej obserwacji zapytania."""

    offer_index: int
    unit_name: str
    rate_category: str
    total_price_grosze: int | None
    avg_night_price_grosze: int | None
    displayed_price_grosze: int
    currency: str
    price_completeness: str
    meal_plan: str
    prepayment: str
    free_cancellation_until: date | None = None
    selected_for_category: bool
    comparable: bool


class StalePrice(ReportModel):
    """Ostatnia znana porównywalna cena z wcześniejszego cyklu."""

    cycle_id: str
    observed_at: datetime
    total_price_grosze: int
    avg_night_price_grosze: int | None
    currency: str
    rate_category: str
    unit_name: str


class QueryResult(ReportModel):
    """Wynik jednego zapytania (konkurent × przyjazd × noce) w bieżącym cyklu."""

    query_id: str
    competitor_id: str
    checkin: date
    checkout: date
    nights: int
    job_status: str
    attempts: int
    availability: str
    technical_reason: str | None = None
    message: str | None = None
    observed_at: datetime | None = None
    lowest: OfferView | None = None
    comparable: bool = False
    non_comparable_reason: str | None = None
    offers: tuple[OfferView, ...] = ()
    stale: StalePrice | None = None

    def comparable_offer(self, mode: str) -> OfferView | None:
        """Cena wnoszona do statystyki w danym trybie (najwyżej jedna na konkurenta)."""
        return comparable_offer(self.offers, mode)


class CalendarCell(ReportModel):
    checkin: date
    nights: int
    mode: str
    n_comparable: int
    n_competitors: int
    min_total_grosze: int | None
    median_total_grosze: int | None
    min_avg_night_grosze: int | None
    median_avg_night_grosze: int | None
    small_sample: bool


class HistoryPoint(ReportModel):
    cycle_id: str
    observed_at: datetime
    availability: str
    total_price_grosze: int | None


class HistorySeries(ReportModel):
    query_id: str
    competitor_id: str
    checkin: date
    nights: int
    points: tuple[HistoryPoint, ...]
    change_vs_previous_grosze: int | None = None
    change_vs_previous_pct: float | None = None


class CompetitorCoverage(ReportModel):
    competitor_id: str
    total: int
    completed: int
    comparable: int
    failed: int
    expired: int
    missing: int


class DataState(ReportModel):
    jobs_total: int
    executed: int
    missing_pending: int
    missing_retry_wait: int
    missing_running: int
    expired: int
    failed: int
    failed_by_reason: dict[str, int]
    available_comparable: int
    unavailable: int
    non_comparable_partial_price: int
    non_comparable_no_matching_offer: int
    non_comparable_other: int
    per_competitor: tuple[CompetitorCoverage, ...]

    @property
    def missing(self) -> int:
        return self.missing_pending + self.missing_retry_wait + self.missing_running


class Overview(ReportModel):
    apartment_id: str
    apartment_name: str
    cycle_id: str
    schedule_slot: int
    cycle_date: date
    start_date: date
    end_date_exclusive: date
    stay_nights: tuple[int, ...]
    status: str
    blocked_reason: str | None = None
    blocked_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    oldest_observed_at: datetime | None = None
    newest_observed_at: datetime | None = None
    currency: str
    adults: int
    children: int
    units: int
    competitors_total: int
    jobs_total: int
    job_counts: dict[str, int]
    coverage_pct: float
    observations_total: int
    comparable_observations: int
    non_comparable_observations: int
    partial_quality: bool
    parser_versions: tuple[str, ...]
    code_sha: str | None = None
    config_hash: str


class CycleReport(ReportModel):
    generated_at: datetime
    timezone: str
    small_sample_threshold: int = SMALL_SAMPLE_THRESHOLD
    overview: Overview
    competitors: tuple[CompetitorInfo, ...]
    results: tuple[QueryResult, ...]
    calendar: tuple[CalendarCell, ...]
    history_cycles: tuple[str, ...]
    history: tuple[HistorySeries, ...]
    data_state: DataState

    def cells(self, mode: str) -> list[CalendarCell]:
        return [c for c in self.calendar if c.mode == mode]

    def cell(self, checkin: date, nights: int, mode: str = MODE_LOWEST) -> CalendarCell | None:
        for c in self.calendar:
            if c.checkin == checkin and c.nights == nights and c.mode == mode:
                return c
        return None

    def result(self, competitor_id: str, checkin: date, nights: int) -> QueryResult | None:
        for r in self.results:
            if r.competitor_id == competitor_id and r.checkin == checkin and r.nights == nights:
                return r
        return None


class ReportError(LookupError):
    """Brak cyklu albo niespójne dane w bazie roboczej."""


# --------------------------------------------------------------------------- reguły porównań


def comparable_offer(
    offers: Iterable[OfferView],
    mode: str,
    *,
    meal_plan: str | None = None,
    prepayment: str | None = None,
) -> OfferView | None:
    """Cena konkurenta dla statystyki w trybie `mode`.

    Bez filtrów warunków: tylko oferty wybrane w swojej kategorii (A§3).
    Z filtrem wyżywienia/przedpłaty (porównanie jednakowych warunków): najtańsza
    porównywalna oferta spełniająca warunki; wartość `unknown` nigdy nie pasuje
    do wartości znanej.
    """
    filtered = meal_plan is not None or prepayment is not None
    candidates = []
    for offer in offers:
        if not offer.comparable:
            continue
        if not filtered and not offer.selected_for_category:
            continue
        if mode != MODE_LOWEST and offer.rate_category != mode:
            continue
        if meal_plan is not None and offer.meal_plan != meal_plan:
            continue
        if prepayment is not None and offer.prepayment != prepayment:
            continue
        candidates.append(offer)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda o: (o.total_price_grosze or 0, o.avg_night_price_grosze or 0, o.offer_index),
    )


def _cell_stats(
    checkin: date, nights: int, mode: str, offers: Sequence[OfferView], n_competitors: int
) -> CalendarCell:
    totals = [o.total_price_grosze for o in offers if o.total_price_grosze is not None]
    avgs = [o.avg_night_price_grosze for o in offers if o.avg_night_price_grosze is not None]
    return CalendarCell(
        checkin=checkin,
        nights=nights,
        mode=mode,
        n_comparable=len(totals),
        n_competitors=n_competitors,
        min_total_grosze=min(totals) if totals else None,
        median_total_grosze=median_grosze(totals),
        min_avg_night_grosze=min(avgs) if avgs else None,
        median_avg_night_grosze=median_grosze(avgs),
        small_sample=len(totals) < SMALL_SAMPLE_THRESHOLD,
    )


# --------------------------------------------------------------------------- odczyt bazy


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _load_offers(
    conn: sqlite3.Connection, observation_ids: Sequence[str], currency: str
) -> dict[str, list[OfferView]]:
    """Oferty pasujące do filtra lokalu, pogrupowane wg obserwacji."""
    result: dict[str, list[OfferView]] = defaultdict(list)
    if not observation_ids:
        return result
    chunk = 500
    for start in range(0, len(observation_ids), chunk):
        ids = observation_ids[start : start + chunk]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT observation_id, offer_index, unit_name, rate_category, total_price_grosze,
                   avg_night_price_grosze, displayed_price_grosze, currency, price_completeness,
                   meal_plan, prepayment, free_cancellation_until, selected_for_category
            FROM offers
            WHERE matches_unit_filter = 1 AND observation_id IN ({placeholders})
            ORDER BY observation_id, offer_index
            """,
            ids,
        ).fetchall()
        for row in rows:
            total = row["total_price_grosze"]
            selected = bool(row["selected_for_category"])
            comparable = (
                row["price_completeness"] == PriceCompleteness.COMPLETE.value
                and total is not None
                and row["currency"] == currency
            )
            result[row["observation_id"]].append(
                OfferView(
                    offer_index=row["offer_index"],
                    unit_name=row["unit_name"],
                    rate_category=row["rate_category"],
                    total_price_grosze=total,
                    avg_night_price_grosze=row["avg_night_price_grosze"],
                    displayed_price_grosze=row["displayed_price_grosze"],
                    currency=row["currency"],
                    price_completeness=row["price_completeness"],
                    meal_plan=row["meal_plan"],
                    prepayment=row["prepayment"],
                    free_cancellation_until=_d(row["free_cancellation_until"]),
                    selected_for_category=selected,
                    comparable=comparable,
                )
            )
    return result


def _non_comparable_reason(availability: str, offers: Sequence[OfferView]) -> str | None:
    if availability == Availability.NO_MATCHING_OFFER.value:
        return "no_matching_offer"
    if availability != Availability.AVAILABLE.value:
        return None
    if comparable_offer(offers, MODE_LOWEST) is not None:
        return None
    selected = [o for o in offers if o.selected_for_category] or list(offers)
    if not selected:
        return "no_matching_offer"
    if any(o.price_completeness != PriceCompleteness.COMPLETE.value for o in selected):
        return "partial_price"
    if any(o.total_price_grosze is not None for o in selected):
        return "currency_mismatch"
    return "partial_price"


def build_cycle_report(
    conn: sqlite3.Connection,
    cycle_id: str,
    *,
    now: datetime,
    tz: ZoneInfo,
    history_cycles: int = DEFAULT_HISTORY_CYCLES,
) -> CycleReport:
    """Buduje model raportu cyklu z roboczej bazy SQLite."""
    if now.tzinfo is None:
        raise ValueError("now musi mieć strefę czasową")
    conn.row_factory = sqlite3.Row

    cycle = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    if cycle is None:
        raise ReportError(f"brak cyklu {cycle_id} w bazie roboczej")
    profile = conn.execute(
        "SELECT * FROM search_profiles WHERE profile_hash = ?", (cycle["profile_hash"],)
    ).fetchone()
    if profile is None:
        raise ReportError(f"brak profilu {cycle['profile_hash']} dla cyklu {cycle_id}")
    currency = profile["currency"]
    apartment = conn.execute("SELECT * FROM own_apartment ORDER BY id LIMIT 1").fetchone()

    competitors = tuple(
        CompetitorInfo(
            competitor_id=row["competitor_id"],
            name=row["name"],
            canonical_url=row["canonical_url"],
            unit_filter=row["unit_filter"],
        )
        for row in conn.execute(
            """
            SELECT cc.competitor_id, cc.name, cc.unit_filter, c.canonical_url
            FROM cycle_competitors cc JOIN competitors c ON c.id = cc.competitor_id
            WHERE cc.cycle_id = ? ORDER BY cc.competitor_id
            """,
            (cycle_id,),
        )
    )
    n_competitors = len(competitors)

    jobs = conn.execute(
        """
        SELECT j.id, j.query_id, j.status, j.attempts, j.last_reason, j.last_message, j.sort_key,
               q.competitor_id, q.checkin, q.checkout, q.nights
        FROM jobs j JOIN stay_queries q ON q.id = j.query_id
        WHERE j.cycle_id = ?
        ORDER BY q.checkin, q.nights, q.competitor_id
        """,
        (cycle_id,),
    ).fetchall()
    query_ids = [row["query_id"] for row in jobs]

    # Najnowsza obserwacja każdego zapytania w bieżącym cyklu.
    current_obs: dict[str, sqlite3.Row] = {}
    for row in conn.execute(
        "SELECT * FROM observations WHERE cycle_id = ? ORDER BY observed_at, id", (cycle_id,)
    ):
        current_obs[row["query_id"]] = row
    parser_versions = tuple(
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT parser_version FROM observations WHERE cycle_id = ? ORDER BY 1",
            (cycle_id,),
        )
    )

    # Obserwacje wcześniejszych cykli (historia i ceny nieaktualne).
    previous_cycles = conn.execute(
        """
        SELECT id FROM cycles
        WHERE schedule_slot < ? OR (schedule_slot = ? AND created_at < ?)
        ORDER BY schedule_slot DESC, created_at DESC
        LIMIT ?
        """,
        (
            cycle["schedule_slot"],
            cycle["schedule_slot"],
            cycle["created_at"],
            max(history_cycles - 1, 0),
        ),
    ).fetchall()
    history_cycle_ids = [r["id"] for r in reversed(previous_cycles)] + [cycle_id]
    cycle_order = {cid: i for i, cid in enumerate(history_cycle_ids)}
    past_obs: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    if len(history_cycle_ids) > 1 and query_ids:
        placeholders = ",".join("?" * (len(history_cycle_ids) - 1))
        for row in conn.execute(
            f"""
            SELECT o.* FROM observations o
            JOIN jobs j ON j.query_id = o.query_id AND j.cycle_id = ?
            WHERE o.cycle_id IN ({placeholders})
            ORDER BY o.observed_at, o.id
            """,
            (cycle_id, *history_cycle_ids[:-1]),
        ):
            past_obs[row["query_id"]][row["cycle_id"]] = row

    observation_ids = [row["id"] for row in current_obs.values()] + [
        row["id"] for per_cycle in past_obs.values() for row in per_cycle.values()
    ]
    offers_by_obs = _load_offers(conn, observation_ids, currency)

    results: list[QueryResult] = []
    history: list[HistorySeries] = []
    for job in jobs:
        query_id = job["query_id"]
        obs = current_obs.get(query_id)
        offers = tuple(offers_by_obs.get(obs["id"], ())) if obs is not None else ()
        availability = obs["availability"] if obs is not None else Availability.UNKNOWN.value
        lowest = comparable_offer(offers, MODE_LOWEST) if obs is not None else None
        if obs is not None and availability != Availability.AVAILABLE.value:
            lowest = None

        stale: StalePrice | None = None
        series_points: list[HistoryPoint] = []
        for past_cycle_id in history_cycle_ids[:-1]:
            past = past_obs.get(query_id, {}).get(past_cycle_id)
            if past is None:
                continue
            past_lowest = (
                comparable_offer(offers_by_obs.get(past["id"], ()), MODE_LOWEST)
                if past["availability"] == Availability.AVAILABLE.value
                else None
            )
            series_points.append(
                HistoryPoint(
                    cycle_id=past_cycle_id,
                    observed_at=_dt(past["observed_at"]),
                    availability=past["availability"],
                    total_price_grosze=past_lowest.total_price_grosze if past_lowest else None,
                )
            )
            if past_lowest is not None and past_lowest.total_price_grosze is not None:
                stale = StalePrice(
                    cycle_id=past_cycle_id,
                    observed_at=_dt(past["observed_at"]),
                    total_price_grosze=past_lowest.total_price_grosze,
                    avg_night_price_grosze=past_lowest.avg_night_price_grosze,
                    currency=past_lowest.currency,
                    rate_category=past_lowest.rate_category,
                    unit_name=past_lowest.unit_name,
                )
        if obs is not None:
            stale = None  # bieżąca obserwacja istnieje — stara cena zostaje tylko w historii
            series_points.append(
                HistoryPoint(
                    cycle_id=cycle_id,
                    observed_at=_dt(obs["observed_at"]),
                    availability=availability,
                    total_price_grosze=lowest.total_price_grosze if lowest else None,
                )
            )

        technical_reason = None
        message = obs["message"] if obs is not None else None
        if job["status"] != JobStatus.COMPLETED.value:
            technical_reason = job["last_reason"]
            message = job["last_message"] or message

        results.append(
            QueryResult(
                query_id=query_id,
                competitor_id=job["competitor_id"],
                checkin=_d(job["checkin"]),
                checkout=_d(job["checkout"]),
                nights=job["nights"],
                job_status=job["status"],
                attempts=job["attempts"],
                availability=availability,
                technical_reason=technical_reason,
                message=message,
                observed_at=_dt(obs["observed_at"]) if obs is not None else None,
                lowest=lowest,
                comparable=lowest is not None,
                non_comparable_reason=(
                    _non_comparable_reason(availability, offers) if obs is not None else None
                ),
                offers=offers,
                stale=stale,
            )
        )

        # Historia ma sens tylko dla zapytań obserwowanych w co najmniej dwóch cyklach.
        if len(series_points) >= 2:
            change = None
            change_pct = None
            if (
                series_points[-1].cycle_id == cycle_id
                and series_points[-1].total_price_grosze is not None
                and series_points[-2].total_price_grosze is not None
            ):
                prev = series_points[-2].total_price_grosze
                change = series_points[-1].total_price_grosze - prev
                change_pct = round(change * 100 / prev, 1) if prev else None
            history.append(
                HistorySeries(
                    query_id=query_id,
                    competitor_id=job["competitor_id"],
                    checkin=_d(job["checkin"]),
                    nights=job["nights"],
                    points=tuple(sorted(series_points, key=lambda p: cycle_order[p.cycle_id])),
                    change_vs_previous_grosze=change,
                    change_vs_previous_pct=change_pct,
                )
            )

    calendar = _build_calendar(results, cycle, n_competitors)
    data_state = _build_data_state(results, competitors)
    overview = _build_overview(
        cycle, apartment, profile, results, data_state, n_competitors, currency, parser_versions
    )
    return CycleReport(
        generated_at=now,
        timezone=tz.key,
        overview=overview,
        competitors=competitors,
        results=tuple(results),
        calendar=calendar,
        history_cycles=tuple(history_cycle_ids),
        history=tuple(history),
        data_state=data_state,
    )


def _build_calendar(
    results: Sequence[QueryResult], cycle: sqlite3.Row, n_competitors: int
) -> tuple[CalendarCell, ...]:
    by_cell: dict[tuple[date, int], list[QueryResult]] = defaultdict(list)
    for result in results:
        by_cell[(result.checkin, result.nights)].append(result)
    nights_list = json.loads(cycle["stay_nights"])
    start = _d(cycle["start_date"])
    end = _d(cycle["end_date_exclusive"])
    days = (end - start).days
    cells: list[CalendarCell] = []
    for mode in RATE_MODES:
        for offset in range(days):
            checkin = date.fromordinal(start.toordinal() + offset)
            for nights in nights_list:
                per_competitor: dict[str, OfferView] = {}
                for result in by_cell.get((checkin, nights), ()):
                    if result.availability != Availability.AVAILABLE.value:
                        continue
                    offer = result.comparable_offer(mode)
                    if offer is not None and result.competitor_id not in per_competitor:
                        per_competitor[result.competitor_id] = offer
                cells.append(
                    _cell_stats(checkin, nights, mode, list(per_competitor.values()), n_competitors)
                )
    return tuple(cells)


def _build_data_state(
    results: Sequence[QueryResult], competitors: Sequence[CompetitorInfo]
) -> DataState:
    status_counts = Counter(r.job_status for r in results)
    failed_by_reason = Counter(
        (r.technical_reason or "unknown") for r in results if r.job_status == JobStatus.FAILED.value
    )
    observed = [r for r in results if r.observed_at is not None]
    per_competitor = []
    for competitor in competitors:
        own = [r for r in results if r.competitor_id == competitor.competitor_id]
        per_competitor.append(
            CompetitorCoverage(
                competitor_id=competitor.competitor_id,
                total=len(own),
                completed=sum(r.job_status == JobStatus.COMPLETED.value for r in own),
                comparable=sum(r.comparable for r in own),
                failed=sum(r.job_status == JobStatus.FAILED.value for r in own),
                expired=sum(r.job_status == JobStatus.EXPIRED.value for r in own),
                missing=sum(r.job_status in MISSING_JOB_STATUSES for r in own),
            )
        )
    return DataState(
        jobs_total=len(results),
        executed=status_counts.get(JobStatus.COMPLETED.value, 0),
        missing_pending=status_counts.get(JobStatus.PENDING.value, 0),
        missing_retry_wait=status_counts.get(JobStatus.RETRY_WAIT.value, 0),
        missing_running=status_counts.get(JobStatus.RUNNING.value, 0),
        expired=status_counts.get(JobStatus.EXPIRED.value, 0),
        failed=status_counts.get(JobStatus.FAILED.value, 0),
        failed_by_reason=dict(sorted(failed_by_reason.items())),
        available_comparable=sum(r.comparable for r in observed),
        unavailable=sum(r.availability == Availability.UNAVAILABLE.value for r in observed),
        non_comparable_partial_price=sum(
            r.non_comparable_reason == "partial_price" for r in observed
        ),
        non_comparable_no_matching_offer=sum(
            r.non_comparable_reason == "no_matching_offer" for r in observed
        ),
        non_comparable_other=sum(
            r.non_comparable_reason not in {None, "partial_price", "no_matching_offer"}
            for r in observed
        ),
        per_competitor=tuple(per_competitor),
    )


def _build_overview(
    cycle: sqlite3.Row,
    apartment: sqlite3.Row | None,
    profile: sqlite3.Row,
    results: Sequence[QueryResult],
    data_state: DataState,
    n_competitors: int,
    currency: str,
    parser_versions: tuple[str, ...],
) -> Overview:
    observed_times = [r.observed_at for r in results if r.observed_at is not None]
    job_counts = {status.value: 0 for status in JobStatus}
    for result in results:
        job_counts[result.job_status] = job_counts.get(result.job_status, 0) + 1
    total = len(results)
    status = cycle["status"]
    observed = [r for r in results if r.observed_at is not None]
    return Overview(
        apartment_id=apartment["id"] if apartment else "",
        apartment_name=apartment["name"] if apartment else "",
        cycle_id=cycle["id"],
        schedule_slot=cycle["schedule_slot"],
        cycle_date=_d(cycle["cycle_date"]),
        start_date=_d(cycle["start_date"]),
        end_date_exclusive=_d(cycle["end_date_exclusive"]),
        stay_nights=tuple(json.loads(cycle["stay_nights"])),
        status=status,
        blocked_reason=cycle["blocked_reason"],
        blocked_message=cycle["blocked_message"],
        created_at=_dt(cycle["created_at"]),
        started_at=_dt(cycle["started_at"]),
        finished_at=_dt(cycle["finished_at"]),
        oldest_observed_at=min(observed_times) if observed_times else None,
        newest_observed_at=max(observed_times) if observed_times else None,
        currency=currency,
        adults=profile["adults"],
        children=len(json.loads(profile["children_ages"] or "[]")),
        units=profile["units"],
        competitors_total=n_competitors,
        jobs_total=total,
        job_counts=job_counts,
        coverage_pct=round(data_state.executed * 100 / total, 1) if total else 0.0,
        observations_total=len(observed),
        comparable_observations=data_state.available_comparable,
        non_comparable_observations=len(observed) - data_state.available_comparable,
        partial_quality=status in PARTIAL_QUALITY_STATUSES,
        parser_versions=parser_versions,
        code_sha=cycle["code_sha"],
        config_hash=cycle["config_hash"],
    )

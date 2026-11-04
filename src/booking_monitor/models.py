"""Wspólne typy domenowe — kontrakt między modułami (ARCHITEKTURA.md §3, §9).

Kwoty są liczbami całkowitymi w groszach (najmniejszej jednostce waluty).
Daty pobytów to `date`, momenty pomiaru to `datetime` w UTC (strefa obowiązkowa).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    """Niezmienny model bez dodatkowych pól."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- statusy


class CycleStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    PARTIAL = "partial"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    BLOCKED = "blocked"


FINISHED_CYCLE_STATUSES = frozenset({CycleStatus.COMPLETED, CycleStatus.COMPLETED_WITH_ERRORS})


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.EXPIRED})


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NO_MATCHING_OFFER = "no_matching_offer"
    UNKNOWN = "unknown"


class TechnicalReason(StrEnum):
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    BLOCKED = "blocked"
    PARSE_ERROR = "parse_error"
    CONTEXT_MISMATCH = "context_mismatch"


class RateCategory(StrEnum):
    NON_REFUNDABLE = "non_refundable"
    FREE_CANCELLATION = "free_cancellation"
    OTHER = "other"
    UNKNOWN = "unknown"


class PriceCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class MealPlan(StrEnum):
    ROOM_ONLY = "room_only"
    BREAKFAST = "breakfast"
    HALF_BOARD = "half_board"
    FULL_BOARD = "full_board"
    ALL_INCLUSIVE = "all_inclusive"
    UNKNOWN = "unknown"


class Prepayment(StrEnum):
    NO_PREPAYMENT = "no_prepayment"
    PREPAYMENT_REQUIRED = "prepayment_required"
    UNKNOWN = "unknown"


class ComponentKind(StrEnum):
    VAT = "vat"
    CITY_TAX = "city_tax"
    CLEANING = "cleaning"
    SERVICE = "service"
    OTHER_TAX = "other_tax"
    OTHER_FEE = "other_fee"
    DEPOSIT = "deposit"  # zwrotna kaucja — nigdy nie wchodzi do ceny pobytu


class ChargeBasis(StrEnum):
    PER_STAY = "per_stay"
    PER_NIGHT = "per_night"
    PER_PERSON = "per_person"
    PER_PERSON_PER_NIGHT = "per_person_per_night"
    PERCENT = "percent"  # procent od kwoty widocznej
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- zapytania

DEFAULT_SOURCE = "booking_browser"


def make_query_id(
    competitor_id: str, checkin: date, checkout: date, profile_hash: str, source: str
) -> str:
    """Stabilny identyfikator zapytania — unikalny dla (konkurent, daty, profil, źródło)."""
    raw = "|".join([competitor_id, checkin.isoformat(), checkout.isoformat(), profile_hash, source])
    return "q_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def make_job_id(cycle_id: str, query_id: str) -> str:
    return f"{cycle_id}:{query_id}"


def make_attempt_id(job_id: str, attempt_no: int) -> str:
    """Deterministyczny identyfikator próby — duplikat próby łamie unikalność w bazie."""
    return f"{job_id}#{attempt_no}"


class StayQuery(FrozenModel):
    """Jednostka zapytania: konkurent × check-in × liczba nocy × profil (A§3)."""

    query_id: str
    competitor_id: str
    checkin: date
    checkout: date
    nights: int = Field(ge=1)
    profile_hash: str
    source: str = DEFAULT_SOURCE

    @classmethod
    def build(
        cls,
        competitor_id: str,
        checkin: date,
        nights: int,
        profile_hash: str,
        source: str = DEFAULT_SOURCE,
    ) -> StayQuery:
        from datetime import timedelta

        checkout = checkin + timedelta(days=nights)
        return cls(
            query_id=make_query_id(competitor_id, checkin, checkout, profile_hash, source),
            competitor_id=competitor_id,
            checkin=checkin,
            checkout=checkout,
            nights=nights,
            profile_hash=profile_hash,
            source=source,
        )


# --------------------------------------------------------------------------- oferty


class PriceComponentIn(FrozenModel):
    """Składnik ceny odczytany ze strony (podatek, opłata, kaucja)."""

    name: str
    kind: ComponentKind
    basis: ChargeBasis
    # Kwota jednostkowa zgodna z `basis` (np. za osobę za noc). None = kwota nieznana.
    amount_grosze: int | None = None
    # Dla basis == PERCENT: stawka w procentach, np. Decimal("8").
    rate_percent: Decimal | None = None
    mandatory: bool = True
    # True = już wliczony w kwotę widoczną (nie doliczamy drugi raz).
    included: bool


class RawOffer(FrozenModel):
    """Jeden wiersz oferty (lokal × taryfa) z sekcji dostępności — wynik parsera."""

    unit_name: str
    unit_id: str | None = None
    is_entire_apartment: bool | None = None
    max_occupancy: int | None = None
    units: int = 1
    rate_name: str | None = None
    currency: str
    displayed_price_grosze: int = Field(ge=0)
    original_price_grosze: int | None = None
    discounts: tuple[str, ...] = ()
    rate_category: RateCategory = RateCategory.UNKNOWN
    free_cancellation_until: date | None = None
    meal_plan: MealPlan = MealPlan.UNKNOWN
    prepayment: Prepayment = Prepayment.UNKNOWN
    components: tuple[PriceComponentIn, ...] = ()
    # False, gdy strona sygnalizuje dodatkowe obowiązkowe opłaty bez możliwej do odczytania kwoty.
    fees_fully_disclosed: bool = True


class PriceComponent(PriceComponentIn):
    """Składnik ceny po przeliczeniu na cały pobyt dla profilu gości."""

    stay_amount_grosze: int | None = None


class Offer(FrozenModel):
    """Oferta po normalizacji (A§3) — zapisywana w partiach i w tabeli `offers`."""

    offer_index: int
    unit_name: str
    unit_id: str | None = None
    is_entire_apartment: bool | None = None
    max_occupancy: int | None = None
    units: int = 1
    rate_name: str | None = None
    rate_category: RateCategory
    free_cancellation_until: date | None = None
    meal_plan: MealPlan
    prepayment: Prepayment
    discounts: tuple[str, ...] = ()
    currency: str
    displayed_price_grosze: int
    original_price_grosze: int | None = None
    total_price_grosze: int | None
    avg_night_price_grosze: int | None
    price_completeness: PriceCompleteness
    deposit_grosze: int | None = None
    # Czy oferta spełnia warunki porównania: cały apartament, filtr lokalu, liczba jednostek.
    matches_unit_filter: bool
    # Najtańsza pasująca oferta w swojej kategorii taryfy dla danej obserwacji.
    selected_for_category: bool = False
    components: tuple[PriceComponent, ...] = ()


# --------------------------------------------------------------------------- parser


class PageContext(FrozenModel):
    """Parametry odczytane z wyrenderowanej strony (A§10 pkt 2)."""

    checkin: date | None = None
    checkout: date | None = None
    adults: int | None = None
    children: int | None = None
    rooms: int | None = None
    currency: str | None = None


class ParsedPage(FrozenModel):
    """Wynik parsera — czysta funkcja na HTML (PLAN_DZIALANIA.md P2).

    - `technical_reason` ustawiony → strona nie nadaje się do interpretacji
      (BLOCKED dla CAPTCHA/blokady, PARSE_ERROR dla nierozpoznanej struktury).
    - `availability` = AVAILABLE tylko gdy odczytano co najmniej jedną ofertę,
      UNAVAILABLE tylko przy jednoznacznym komunikacie, w pozostałych przypadkach UNKNOWN.
    """

    availability: Availability
    technical_reason: TechnicalReason | None = None
    context: PageContext = PageContext()
    offers: tuple[RawOffer, ...] = ()
    message: str | None = None
    parser_version: str
    property_name: str | None = None
    booking_property_id: str | None = None


# --------------------------------------------------------------------------- wynik próby


class ObservationOutcome(FrozenModel):
    """Wynik jednej próby odczytu (provider → parser → normalizacja).

    Dokładnie jedno z: `availability` (strona zinterpretowana) albo `technical_reason`.
    """

    availability: Availability | None = None
    technical_reason: TechnicalReason | None = None
    offers: tuple[Offer, ...] = ()
    message: str | None = None
    parser_version: str | None = None
    source_url: str | None = None
    observed_at: datetime
    retry_after_seconds: float | None = None
    diagnostics: tuple[str, ...] = ()

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at musi mieć strefę czasową (UTC)")
        return value

    @model_validator(mode="after")
    def _exactly_one_result(self) -> ObservationOutcome:
        if (self.availability is None) == (self.technical_reason is None):
            raise ValueError("ustaw dokładnie jedno z: availability, technical_reason")
        if self.technical_reason is not None and self.offers:
            raise ValueError("błąd techniczny nie może zawierać ofert")
        return self

    @property
    def is_technical_error(self) -> bool:
        return self.technical_reason is not None

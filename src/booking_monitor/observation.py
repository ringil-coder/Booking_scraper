"""Jedna obserwacja: provider → parser → kontrola kontekstu → normalizacja (A§10).

Wynik to zawsze `ObservationOutcome`: albo zinterpretowana dostępność, albo
przyczyna techniczna. Błąd parsera lub blokada nigdy nie są niedostępnością.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from booking_monitor.config import Profile
from booking_monitor.models import (
    Availability,
    ObservationOutcome,
    PageContext,
    ParsedPage,
    StayQuery,
    TechnicalReason,
)
from booking_monitor.normalization import normalize_offers
from booking_monitor.providers.base import FetchError, PageProvider
from booking_monitor.storage.state import CompetitorSnapshot

Parser = Callable[..., ParsedPage]


class Observer(Protocol):
    """Wykonuje jedną próbę odczytu zapytania. Runner zna tylko ten interfejs."""

    def __call__(self, competitor: CompetitorSnapshot, query: StayQuery) -> ObservationOutcome: ...


def verify_context(context: PageContext, *, query: StayQuery, profile: Profile) -> str | None:
    """Opis niezgodności parametrów strony z zapytaniem albo None, gdy wszystko się zgadza.

    Daty i waluta muszą być potwierdzone na stronie (sam adres URL nie wystarcza).
    Liczby gości i jednostek sprawdzamy, jeśli strona je pokazuje.
    """
    problems: list[str] = []
    required = {
        "check-in": (context.checkin, query.checkin),
        "check-out": (context.checkout, query.checkout),
        "waluta": (context.currency, profile.currency),
    }
    for label, (seen, expected) in required.items():
        if seen is None:
            problems.append(f"nie potwierdzono: {label}")
        elif seen != expected:
            problems.append(f"{label}: strona={seen}, zapytanie={expected}")
    optional = {
        "dorośli": (context.adults, profile.adults),
        "dzieci": (context.children, len(profile.children_ages)),
        "jednostki": (context.rooms, profile.units),
    }
    for label, (seen, expected) in optional.items():
        if seen is not None and seen != expected:
            problems.append(f"{label}: strona={seen}, zapytanie={expected}")
    return "; ".join(problems) or None


def interpret_page(
    parsed: ParsedPage,
    *,
    query: StayQuery,
    profile: Profile,
    unit_filter: str | None,
    observed_at: datetime,
    source_url: str | None,
    diagnostics: tuple[str, ...] = (),
) -> ObservationOutcome:
    """Zamienia wynik parsera na wynik próby."""
    common = {
        "observed_at": observed_at,
        "source_url": source_url,
        "parser_version": parsed.parser_version,
        "diagnostics": diagnostics,
    }
    if parsed.technical_reason is not None:
        return ObservationOutcome(
            technical_reason=parsed.technical_reason, message=parsed.message, **common
        )
    mismatch = verify_context(parsed.context, query=query, profile=profile)
    if mismatch:
        return ObservationOutcome(
            technical_reason=TechnicalReason.CONTEXT_MISMATCH, message=mismatch, **common
        )
    if parsed.availability is Availability.UNAVAILABLE:
        return ObservationOutcome(
            availability=Availability.UNAVAILABLE, message=parsed.message, **common
        )
    if parsed.availability is not Availability.AVAILABLE or not parsed.offers:
        # Brak jednoznacznego wyniku to błąd interpretacji, a nie niedostępność.
        return ObservationOutcome(
            technical_reason=TechnicalReason.PARSE_ERROR,
            message=parsed.message or "brak ofert i brak komunikatu o niedostępności",
            **common,
        )
    offers = normalize_offers(
        parsed.offers, nights=query.nights, profile=profile, unit_filter=unit_filter
    )
    availability = (
        Availability.AVAILABLE
        if any(o.matches_unit_filter for o in offers)
        else Availability.NO_MATCHING_OFFER
    )
    return ObservationOutcome(
        availability=availability, offers=offers, message=parsed.message, **common
    )


class BrowserObserver:
    """Obserwator produkcyjny: provider strony + parser Booking."""

    def __init__(
        self,
        provider: PageProvider,
        profile: Profile,
        *,
        parse: Parser | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if parse is None:
            from booking_monitor.parsing import parse_property_page

            parse = parse_property_page
        self._provider = provider
        self._profile = profile
        self._parse = parse
        self._clock = clock

    def __call__(self, competitor: CompetitorSnapshot, query: StayQuery) -> ObservationOutcome:
        try:
            snapshot = self._provider.fetch(competitor, query, self._profile)
        except FetchError as exc:
            return ObservationOutcome(
                technical_reason=exc.reason,
                message=exc.message,
                observed_at=self._clock(),
                retry_after_seconds=exc.retry_after_seconds,
                diagnostics=exc.diagnostics,
            )
        try:
            parsed = self._parse(snapshot.html, url=snapshot.final_url)
        except Exception as exc:  # parser nie może zatrzymać całej serii
            return ObservationOutcome(
                technical_reason=TechnicalReason.PARSE_ERROR,
                message=f"wyjątek parsera: {type(exc).__name__}: {exc}",
                observed_at=snapshot.fetched_at,
                source_url=snapshot.final_url,
                diagnostics=snapshot.diagnostics,
            )
        return interpret_page(
            parsed,
            query=query,
            profile=self._profile,
            unit_filter=competitor.unit_filter,
            observed_at=snapshot.fetched_at,
            source_url=snapshot.final_url,
            diagnostics=snapshot.diagnostics,
        )

    def close(self) -> None:
        self._provider.close()

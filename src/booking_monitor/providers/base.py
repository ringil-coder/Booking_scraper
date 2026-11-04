"""Kontrakt providera stron (PLAN_DZIALANIA.md P2).

Provider otwiera stronę konkretnej oferty z parametrami zapytania i zwraca
wyrenderowany HTML. Nie interpretuje cen — robi to `booking_monitor.parsing`.
Błędy techniczne zgłasza wyjątkiem `FetchError` z przyczyną z A§9.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from booking_monitor.config import Profile
from booking_monitor.models import StayQuery, TechnicalReason
from booking_monitor.storage.state import CompetitorSnapshot


@dataclass(frozen=True)
class PageSnapshot:
    requested_url: str
    final_url: str
    http_status: int | None
    html: str
    fetched_at: datetime
    timings: dict[str, float] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()


class FetchError(Exception):
    """Błąd techniczny pobrania strony (timeout, sieć, blokada, HTTP 429...)."""

    def __init__(
        self,
        reason: TechnicalReason,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        diagnostics: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        self.diagnostics = diagnostics


class PageProvider(Protocol):
    """Jedna aktywna karta; odstęp między nawigacjami pilnuje implementacja."""

    def fetch(
        self, competitor: CompetitorSnapshot, query: StayQuery, profile: Profile
    ) -> PageSnapshot: ...

    def close(self) -> None: ...

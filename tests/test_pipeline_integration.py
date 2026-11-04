"""Integracja: próbka HTML → parser → kontrola kontekstu → normalizacja (E3 + E2).

Próbki są syntetyczne (tests/fixtures/booking) — weryfikacja na prawdziwych stronach w E1.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from booking_monitor.config import Profile
from booking_monitor.models import Availability, PriceCompleteness, StayQuery, TechnicalReason
from booking_monitor.observation import BrowserObserver
from booking_monitor.providers.base import PageSnapshot
from booking_monitor.storage.state import CompetitorSnapshot
from tests.helpers import utc

FIXTURES = Path(__file__).parent / "fixtures" / "booking"
PROFILE = Profile(adults=2)
COMPETITOR = CompetitorSnapshot(
    competitor_id="alfa",
    name="Alfa",
    booking_url="https://www.booking.com/hotel/pl/alfa.html",
    canonical_url="https://www.booking.com/hotel/pl/alfa.html",
)


class FixtureProvider:
    def __init__(self, name: str):
        self.html = (FIXTURES / name).read_text(encoding="utf-8")

    def fetch(self, competitor, query, profile):
        return PageSnapshot("u", competitor.canonical_url, 200, self.html, utc(2026, 11, 4, 6))

    def close(self):
        pass


def observe(name: str, nights: int = 3):
    query = StayQuery.build("alfa", date(2026, 11, 20), nights, PROFILE.profile_hash())
    return BrowserObserver(FixtureProvider(name), PROFILE)(COMPETITOR, query)


def test_available_page_yields_selected_offers():
    out = observe("available_multi.html")
    assert out.availability is Availability.AVAILABLE
    selected = [o for o in out.offers if o.selected_for_category]
    assert selected and all(o.matches_unit_filter for o in selected)
    assert {o.rate_category for o in selected} >= {"non_refundable", "free_cancellation"}
    assert all(o.currency == "PLN" for o in out.offers)


def test_taxes_excluded_are_added_to_total():
    out = observe("available_taxes_excluded.html")
    assert out.availability is Availability.AVAILABLE
    complete = [o for o in out.offers if o.price_completeness is PriceCompleteness.COMPLETE]
    assert complete and all(o.total_price_grosze > o.displayed_price_grosze for o in complete)


@pytest.mark.parametrize(
    ("fixture", "nights", "availability", "reason"),
    [
        ("unavailable.html", 3, Availability.UNAVAILABLE, None),
        ("min_stay.html", 2, Availability.UNAVAILABLE, None),
        ("captcha_challenge.html", 3, None, TechnicalReason.BLOCKED),
        ("access_denied.html", 3, None, TechnicalReason.BLOCKED),
        ("changed_layout.html", 3, None, TechnicalReason.PARSE_ERROR),
        ("unreadable_price.html", 3, None, TechnicalReason.PARSE_ERROR),
        ("context_mismatch.html", 3, None, TechnicalReason.CONTEXT_MISMATCH),
        ("currency_eur.html", 3, None, TechnicalReason.CONTEXT_MISMATCH),
    ],
)
def test_outcomes(fixture, nights, availability, reason):
    out = observe(fixture, nights)
    assert out.availability == availability
    assert out.technical_reason == reason
    if reason is not None:
        assert out.offers == ()

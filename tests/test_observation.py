from datetime import date

from booking_monitor.config import Profile
from booking_monitor.models import (
    Availability,
    PageContext,
    ParsedPage,
    RateCategory,
    RawOffer,
    StayQuery,
    TechnicalReason,
)
from booking_monitor.observation import BrowserObserver, interpret_page, verify_context
from booking_monitor.providers.base import FetchError, PageSnapshot
from booking_monitor.storage.state import CompetitorSnapshot
from tests.helpers import utc

PROFILE = Profile(adults=2)
QUERY = StayQuery.build("alfa", date(2026, 11, 20), 3, PROFILE.profile_hash())
GOOD_CONTEXT = PageContext(
    checkin=date(2026, 11, 20), checkout=date(2026, 11, 23), adults=2, currency="PLN"
)
COMPETITOR = CompetitorSnapshot(
    competitor_id="alfa",
    name="Alfa",
    booking_url="https://www.booking.com/hotel/pl/alfa.html",
    canonical_url="https://www.booking.com/hotel/pl/alfa.html",
)
NOW = utc(2026, 11, 4, 6)


def page(**kw) -> ParsedPage:
    params = {"availability": Availability.AVAILABLE, "context": GOOD_CONTEXT} | kw
    return ParsedPage(parser_version="t-1", **params)


def raw(**kw) -> RawOffer:
    params = {"unit_name": "Apartament", "currency": "PLN", "displayed_price_grosze": 90_000}
    return RawOffer(**(params | kw))


def interpret(parsed, unit_filter=None):
    return interpret_page(
        parsed,
        query=QUERY,
        profile=PROFILE,
        unit_filter=unit_filter,
        observed_at=NOW,
        source_url="u",
    )


def test_context_must_be_confirmed_on_page():
    assert verify_context(GOOD_CONTEXT, query=QUERY, profile=PROFILE) is None
    missing = verify_context(PageContext(), query=QUERY, profile=PROFILE)
    assert "nie potwierdzono: check-in" in missing and "waluta" in missing
    wrong = PageContext(**(GOOD_CONTEXT.model_dump() | {"currency": "EUR", "adults": 3}))
    problems = verify_context(wrong, query=QUERY, profile=PROFILE)
    assert "waluta: strona=EUR" in problems and "dorośli" in problems


def test_available_with_matching_offer():
    out = interpret(page(offers=(raw(rate_category=RateCategory.NON_REFUNDABLE),)))
    assert out.availability is Availability.AVAILABLE
    assert out.offers[0].selected_for_category
    assert out.offers[0].avg_night_price_grosze == 30_000


def test_no_matching_offer_keeps_offers():
    out = interpret(page(offers=(raw(unit_name="Pokój"),)), unit_filter="Apartament Deluxe")
    assert out.availability is Availability.NO_MATCHING_OFFER
    assert len(out.offers) == 1


def test_unavailable_requires_explicit_message():
    out = interpret(page(availability=Availability.UNAVAILABLE, message="Brak wolnych miejsc"))
    assert out.availability is Availability.UNAVAILABLE
    unclear = interpret(page(availability=Availability.UNKNOWN))
    assert unclear.technical_reason is TechnicalReason.PARSE_ERROR


def test_technical_reasons_never_become_unavailability():
    blocked = interpret(page(availability=Availability.UNKNOWN, technical_reason="blocked"))
    assert blocked.technical_reason is TechnicalReason.BLOCKED and blocked.availability is None
    mismatch = interpret(page(context=PageContext(), offers=(raw(),)))
    assert mismatch.technical_reason is TechnicalReason.CONTEXT_MISMATCH
    assert mismatch.offers == ()


class StubProvider:
    def __init__(self, result):
        self.result = result
        self.closed = False

    def fetch(self, competitor, query, profile):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


def test_browser_observer_maps_fetch_error():
    error = FetchError(TechnicalReason.NETWORK_ERROR, "429", retry_after_seconds=120)
    observer = BrowserObserver(
        StubProvider(error), PROFILE, parse=lambda *a, **k: None, clock=lambda: NOW
    )
    out = observer(COMPETITOR, QUERY)
    assert out.technical_reason is TechnicalReason.NETWORK_ERROR
    assert out.retry_after_seconds == 120


def test_browser_observer_parser_exception_is_parse_error():
    snapshot = PageSnapshot("u", "u2", 200, "<html/>", NOW)

    def boom(html, url=None):
        raise RuntimeError("zmiana strony")

    out = BrowserObserver(StubProvider(snapshot), PROFILE, parse=boom)(COMPETITOR, QUERY)
    assert out.technical_reason is TechnicalReason.PARSE_ERROR
    assert "zmiana strony" in out.message


def test_browser_observer_success_path():
    snapshot = PageSnapshot("u", "u2", 200, "<html/>", NOW)
    parsed = page(offers=(raw(),))
    provider = StubProvider(snapshot)
    observer = BrowserObserver(provider, PROFILE, parse=lambda html, url=None: parsed)
    out = observer(COMPETITOR, QUERY)
    assert out.availability is Availability.AVAILABLE and out.source_url == "u2"
    observer.close()
    assert provider.closed

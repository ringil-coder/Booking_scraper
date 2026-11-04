"""Parser strony oferty na SYNTETYCZNYCH próbkach (A§15 pkt 9).

Próbki w `tests/fixtures/booking/` nie pochodzą z booking.com; po pilotażu E1 należy
dodać zanonimizowane prawdziwe strony i dopasować selektory.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from booking_monitor.models import (
    Availability,
    ChargeBasis,
    ComponentKind,
    MealPlan,
    Prepayment,
    RateCategory,
    TechnicalReason,
)
from booking_monitor.parsing import PARSER_VERSION, parse_property_page
from booking_monitor.parsing.booking import classify_unit

FIXTURES = Path(__file__).parent / "fixtures" / "booking"


def parse(name: str):
    return parse_property_page((FIXTURES / name).read_text(encoding="utf-8"), url=name)


def test_all_fixtures_are_marked_synthetic() -> None:
    for path in FIXTURES.glob("*.html"):
        assert "SYNTETYCZNA PRÓBKA" in path.read_text(encoding="utf-8"), path.name


def test_available_multi_rows_and_context() -> None:
    page = parse("available_multi.html")
    assert page.availability is Availability.AVAILABLE
    assert page.technical_reason is None
    assert page.parser_version == PARSER_VERSION
    assert page.property_name == "Apartamenty Testowe Centrum"
    assert page.booking_property_id == "1234567"

    ctx = page.context
    assert (ctx.checkin, ctx.checkout) == (date(2026, 11, 20), date(2026, 11, 23))
    assert (ctx.adults, ctx.children, ctx.rooms, ctx.currency) == (2, 0, 1, "PLN")

    assert len(page.offers) == 3
    non_refundable, flexible, room = page.offers

    assert non_refundable.unit_name == "Apartament z 1 sypialnią"
    assert non_refundable.unit_id == "123456701"
    assert non_refundable.is_entire_apartment is True
    assert non_refundable.max_occupancy == 4
    assert non_refundable.currency == "PLN"
    assert non_refundable.displayed_price_grosze == 102_000
    assert non_refundable.original_price_grosze == 120_000
    assert non_refundable.discounts == ("Okazja na ostatnią chwilę",)
    assert non_refundable.rate_category is RateCategory.NON_REFUNDABLE
    assert non_refundable.prepayment is Prepayment.PREPAYMENT_REQUIRED
    assert non_refundable.meal_plan is MealPlan.UNKNOWN
    assert non_refundable.components == ()
    assert non_refundable.fees_fully_disclosed is True

    # Drugi wiersz dziedziczy lokal z komórki rowspan.
    assert flexible.unit_name == "Apartament z 1 sypialnią"
    assert flexible.unit_id == "123456701"
    assert flexible.displayed_price_grosze == 120_000
    assert flexible.original_price_grosze is None
    assert flexible.rate_category is RateCategory.FREE_CANCELLATION
    assert flexible.free_cancellation_until == date(2026, 11, 18)
    assert flexible.meal_plan is MealPlan.BREAKFAST
    assert flexible.prepayment is Prepayment.NO_PREPAYMENT

    assert room.unit_name == "Pokój dwuosobowy z łazienką"
    assert room.is_entire_apartment is False
    assert room.max_occupancy == 2
    assert room.displayed_price_grosze == 80_000
    assert room.meal_plan is MealPlan.ROOM_ONLY
    assert room.rate_category is RateCategory.NON_REFUNDABLE


def test_aggregate_taxes_are_not_double_counted() -> None:
    page = parse("available_taxes_excluded.html")
    assert page.availability is Availability.AVAILABLE
    first, second = page.offers
    assert first.unit_name == "Studio"
    assert first.is_entire_apartment is True
    assert first.displayed_price_grosze == 64_000
    # Tylko suma spod ceny; szczegóły „Nie zawiera” opisują tę samą kwotę.
    assert len(first.components) == 1
    aggregate = first.components[0]
    assert aggregate.kind is ComponentKind.OTHER_TAX
    assert aggregate.basis is ChargeBasis.PER_STAY
    assert aggregate.amount_grosze == 9_600
    assert aggregate.included is False

    assert second.components[0].amount_grosze == 10_500
    assert second.rate_category is RateCategory.FREE_CANCELLATION
    assert second.free_cancellation_until == date(2026, 11, 15)
    assert second.prepayment is Prepayment.NO_PREPAYMENT


def test_itemized_fees_percent_and_deposit() -> None:
    page = parse("available_itemized_fees.html")
    assert page.availability is Availability.AVAILABLE
    ctx = page.context
    assert (ctx.checkin, ctx.checkout) == (date(2026, 11, 20), date(2026, 11, 23))
    assert (ctx.adults, ctx.children, ctx.rooms, ctx.currency) == (2, 0, 1, "PLN")
    assert page.booking_property_id == "5550001"

    (offer,) = page.offers
    assert offer.displayed_price_grosze == 123_456
    assert offer.unit_id == "555000101"  # z data-block-id
    assert offer.max_occupancy == 3  # liczba ikon
    assert offer.rate_category is RateCategory.FREE_CANCELLATION
    assert offer.free_cancellation_until == date(2026, 12, 1)

    by_kind = {c.kind: c for c in offer.components}
    assert set(by_kind) == {
        ComponentKind.VAT,
        ComponentKind.CITY_TAX,
        ComponentKind.CLEANING,
        ComponentKind.DEPOSIT,
    }
    vat = by_kind[ComponentKind.VAT]
    assert (vat.basis, vat.rate_percent, vat.included) == (ChargeBasis.PERCENT, Decimal("8"), True)
    city = by_kind[ComponentKind.CITY_TAX]
    assert (city.basis, city.amount_grosze, city.included) == (
        ChargeBasis.PER_PERSON_PER_NIGHT,
        250,
        False,
    )
    cleaning = by_kind[ComponentKind.CLEANING]
    assert (cleaning.basis, cleaning.amount_grosze, cleaning.included) == (
        ChargeBasis.PER_STAY,
        15_000,
        False,
    )
    deposit = by_kind[ComponentKind.DEPOSIT]
    assert (deposit.amount_grosze, deposit.included) == (50_000, False)
    assert offer.fees_fully_disclosed is True


def test_unknown_extra_fees_and_display_context() -> None:
    page = parse("available_unknown_fees.html")
    assert page.availability is Availability.AVAILABLE
    ctx = page.context
    assert (ctx.checkin, ctx.checkout) == (date(2026, 11, 20), date(2026, 11, 23))
    assert (ctx.adults, ctx.children, ctx.rooms, ctx.currency) == (2, 0, 1, "PLN")
    assert page.booking_property_id == "3330001"
    (offer,) = page.offers
    assert offer.fees_fully_disclosed is False
    assert offer.is_entire_apartment is True
    assert offer.rate_category is RateCategory.OTHER


@pytest.mark.parametrize(
    ("fixture", "fragment"),
    [
        ("unavailable.html", "Brak dostępnych miejsc"),
        ("min_stay.html", "Minimalna długość pobytu"),
    ],
)
def test_unavailable_requires_explicit_message(fixture: str, fragment: str) -> None:
    page = parse(fixture)
    assert page.availability is Availability.UNAVAILABLE
    assert page.technical_reason is None
    assert page.offers == ()
    assert fragment in (page.message or "")
    assert page.context.checkin == date(2026, 11, 20)


def test_all_rows_sold_out_is_unavailable() -> None:
    page = parse("all_sold_out.html")
    assert page.availability is Availability.UNAVAILABLE
    assert "wyprzedane" in (page.message or "")


@pytest.mark.parametrize(
    "fixture", ["captcha_challenge.html", "access_denied.html", "verify_human.html"]
)
def test_block_pages(fixture: str) -> None:
    page = parse(fixture)
    assert page.technical_reason is TechnicalReason.BLOCKED
    assert page.availability is Availability.UNKNOWN
    assert page.offers == ()


# Struktura zaobserwowana w pilotażu E1 (odtworzona syntetycznie): zwykła strona oferty
# ładuje integrację AWS WAF, a prawdziwa strona wyzwania (HTTP 202) ma #challenge-container.
_WAF_INTEGRATION = (
    "<script>function loadWaf(){var script=document.createElement('script');"
    "script.src='https://example.edge.sdk.awswaf.com/x/y/challenge.js';"
    "window.awsWafCookieDomainList = ['booking.com'];}</script>"
)
_PROPERTY_WITHOUT_DATES = f"""<html lang="pl"><head><title>Alfa, Warszawa</title>
{_WAF_INTEGRATION}</head><body>
<h2 id="hp_hotel_name">Alfa</h2>
<input type="hidden" name="checkin" value="">
<div data-testid="occupancy-config">2 dorosłych · 0 dzieci · 1 pokój</div>
<div id="hp_availability_style_changes"><div class="bui-alert bui-alert--error">
Wybierz daty, aby zobaczyć dostępność i ceny w obiekcie</div></div>
</body></html>"""
_WAF_CHALLENGE = """<html lang="en"><head><title></title>
<script>window.awsWafCookieDomainList = ['booking.com'];</script>
<script src="https://www.booking.com/__challenge_x/y/z/challenge.js"></script>
</head><body><div id="challenge-container"></div>
<script>AwsWafIntegration.getToken().then(() => { window.location.href = '/'; });</script>
<noscript>In order to continue, we need to verify that you're not a robot.</noscript>
</body></html>"""


def test_waf_integration_script_on_property_page_is_not_a_block() -> None:
    page = parse_property_page(_PROPERTY_WITHOUT_DATES)
    assert page.technical_reason is TechnicalReason.PARSE_ERROR
    assert page.property_name == "Alfa"
    assert "bez dat pobytu" in (page.message or "")


def test_waf_integration_script_with_table_is_not_a_block() -> None:
    html = (FIXTURES / "available_multi.html").read_text(encoding="utf-8")
    page = parse_property_page(html.replace("</head>", _WAF_INTEGRATION + "</head>", 1))
    assert page.technical_reason is None
    assert page.availability is Availability.AVAILABLE


def test_waf_challenge_page_is_blocked() -> None:
    page = parse_property_page(_WAF_CHALLENGE)
    assert page.technical_reason is TechnicalReason.BLOCKED
    assert "#challenge-container" in (page.message or "")


def test_waf_script_without_property_content_is_blocked() -> None:
    html = f"<html><head><title>Booking.com</title>{_WAF_INTEGRATION}</head><body></body></html>"
    page = parse_property_page(html)
    assert page.technical_reason is TechnicalReason.BLOCKED


@pytest.mark.parametrize("fixture", ["changed_layout.html", "unreadable_price.html"])
def test_changed_page_is_parse_error_not_unavailable(fixture: str) -> None:
    page = parse(fixture)
    assert page.technical_reason is TechnicalReason.PARSE_ERROR
    assert page.availability is Availability.UNKNOWN
    assert page.offers == ()


def test_empty_html_is_parse_error() -> None:
    page = parse_property_page("")
    assert page.technical_reason is TechnicalReason.PARSE_ERROR


def test_context_mismatch_is_reported_as_rendered() -> None:
    page = parse("context_mismatch.html")
    assert page.availability is Availability.AVAILABLE
    assert page.context.checkin == date(2026, 11, 21)
    assert page.context.adults == 1


def test_currency_rendered_in_eur() -> None:
    page = parse("currency_eur.html")
    assert page.context.currency == "EUR"
    (offer,) = page.offers
    assert (offer.currency, offer.displayed_price_grosze) == ("EUR", 24_500)


def test_conflicting_hidden_inputs_give_unknown_context() -> None:
    html = """
    <form><input name="checkin" value="2026-11-20"></form>
    <form><input name="checkin" value="2026-11-21"></form>
    """
    page = parse_property_page(html)
    assert page.context.checkin is None


def test_block_signal_ignored_when_table_present() -> None:
    html = (FIXTURES / "available_multi.html").read_text(encoding="utf-8")
    html = html.replace("</body>", "<iframe src='/login-captcha'></iframe></body>")
    page = parse_property_page(html)
    assert page.availability is Availability.AVAILABLE


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Apartament z 2 sypialniami", True),
        ("Apartament z 2 pokojami", True),
        ("Studio z balkonem", True),
        ("Domek letniskowy", True),
        ("Willa z basenem", True),
        ("Pokój dwuosobowy", False),
        ("Pokój w apartamencie", False),
        ("Łóżko w pokoju wieloosobowym", False),
        ("Family Room", False),
        ("Deluxe", None),
    ],
)
def test_classify_unit(name: str, expected: bool | None) -> None:
    assert classify_unit(name) is expected

from decimal import Decimal

import pytest

from booking_monitor.config import Profile
from booking_monitor.models import (
    ChargeBasis,
    ComponentKind,
    MealPlan,
    PriceCompleteness,
    PriceComponentIn,
    RateCategory,
    RawOffer,
)
from booking_monitor.normalization import normalize_offers, unit_matches
from booking_monitor.prices import compute_price, round_grosze

PROFILE = Profile(adults=2, children_ages=(7,))  # 3 gości


def comp(kind, basis, amount=None, *, included=False, mandatory=True, rate=None, name="x"):
    return PriceComponentIn(
        name=name,
        kind=kind,
        basis=basis,
        amount_grosze=amount,
        rate_percent=rate,
        included=included,
        mandatory=mandatory,
    )


def offer(price=100_000, *components, **kw):
    params = {"unit_name": "Apartament z 1 sypialnią", "currency": "PLN"} | kw
    return RawOffer(displayed_price_grosze=price, components=tuple(components), **params)


def test_surcharges_by_basis():
    raw = offer(
        100_000,
        comp(ComponentKind.CLEANING, ChargeBasis.PER_STAY, 8_000),
        comp(ComponentKind.CITY_TAX, ChargeBasis.PER_PERSON_PER_NIGHT, 300),
        comp(ComponentKind.SERVICE, ChargeBasis.PER_NIGHT, 1_000),
        comp(ComponentKind.OTHER_FEE, ChargeBasis.PER_PERSON, 500),
        comp(ComponentKind.VAT, ChargeBasis.PERCENT, rate=Decimal("8")),
    )
    result = compute_price(raw, nights=3, guests=3)
    # 1000 + 80 + 3*3*3 + 3*10 + 3*5 + 8% z 1000
    assert result.total_grosze == 100_000 + 8_000 + 2_700 + 3_000 + 1_500 + 8_000
    assert result.completeness is PriceCompleteness.COMPLETE
    assert result.avg_night_grosze == round_grosze(Decimal(result.total_grosze) / 3)


def test_included_tax_is_not_added_twice():
    raw = offer(
        100_000,
        comp(ComponentKind.VAT, ChargeBasis.PERCENT, rate=Decimal("8"), included=True),
        comp(ComponentKind.CLEANING, ChargeBasis.PER_STAY, 5_000, included=True),
    )
    result = compute_price(raw, nights=2, guests=2)
    assert result.total_grosze == 100_000
    assert result.components[0].stay_amount_grosze == 8_000  # zapisany informacyjnie


def test_unknown_mandatory_fee_makes_price_partial():
    raw = offer(100_000, comp(ComponentKind.CITY_TAX, ChargeBasis.PER_PERSON_PER_NIGHT, None))
    result = compute_price(raw, nights=2, guests=2)
    assert result.total_grosze is None and result.avg_night_grosze is None
    assert result.completeness is PriceCompleteness.PARTIAL


@pytest.mark.parametrize(
    "component",
    [
        comp(ComponentKind.OTHER_FEE, ChargeBasis.UNKNOWN, 1_000),
        comp(ComponentKind.VAT, ChargeBasis.PERCENT, rate=None),
    ],
)
def test_unknown_basis_or_rate_is_partial(component):
    assert compute_price(offer(50_000, component), nights=2, guests=2).total_grosze is None


def test_undisclosed_fees_flag_is_partial():
    raw = offer(100_000, fees_fully_disclosed=False)
    assert compute_price(raw, nights=2, guests=2).completeness is PriceCompleteness.PARTIAL


def test_optional_extra_and_deposit_are_outside_stay_price():
    raw = offer(
        100_000,
        comp(ComponentKind.OTHER_FEE, ChargeBasis.PER_PERSON_PER_NIGHT, 4_000, mandatory=False),
        comp(ComponentKind.DEPOSIT, ChargeBasis.PER_STAY, 50_000),
    )
    result = compute_price(raw, nights=2, guests=2)
    assert result.total_grosze == 100_000
    assert result.deposit_grosze == 50_000


def test_unknown_deposit_does_not_affect_completeness():
    raw = offer(100_000, comp(ComponentKind.DEPOSIT, ChargeBasis.PER_STAY, None))
    result = compute_price(raw, nights=2, guests=2)
    assert result.total_grosze == 100_000 and result.deposit_grosze is None


def test_average_per_night_rounds_half_up():
    # 1000,01 zł / 3 noce = 333,336666... → 333,34 zł
    assert compute_price(offer(100_001), nights=3, guests=2).avg_night_grosze == 33_334
    # 0,05 zł / 2 = 0,025 → 0,03 zł
    assert compute_price(offer(5), nights=2, guests=2).avg_night_grosze == 3


def test_unit_matching_rules():
    assert unit_matches(offer(), profile=PROFILE, unit_filter=None)
    assert unit_matches(offer(is_entire_apartment=None), profile=PROFILE, unit_filter=None)
    assert not unit_matches(offer(is_entire_apartment=False), profile=PROFILE, unit_filter=None)
    assert not unit_matches(offer(max_occupancy=2), profile=PROFILE, unit_filter=None)
    assert not unit_matches(offer(currency="EUR"), profile=PROFILE, unit_filter=None)
    assert not unit_matches(offer(units=2), profile=PROFILE, unit_filter=None)
    assert unit_matches(offer(), profile=PROFILE, unit_filter="1 SYPIALNI")
    assert not unit_matches(offer(), profile=PROFILE, unit_filter="Studio")
    assert unit_matches(offer(unit_id="123"), profile=PROFILE, unit_filter="id:123")
    assert not unit_matches(offer(unit_id="124"), profile=PROFILE, unit_filter="id: 123")


def test_cheapest_matching_offer_selected_per_category():
    offers = normalize_offers(
        [
            offer(90_000, rate_category=RateCategory.NON_REFUNDABLE),
            offer(80_000, rate_category=RateCategory.NON_REFUNDABLE, is_entire_apartment=False),
            offer(95_000, rate_category=RateCategory.NON_REFUNDABLE, meal_plan=MealPlan.BREAKFAST),
            offer(120_000, rate_category=RateCategory.FREE_CANCELLATION),
            offer(110_000, rate_category=RateCategory.FREE_CANCELLATION),
            offer(
                100_000,
                comp(ComponentKind.CITY_TAX, ChargeBasis.PER_STAY, None),
                rate_category=RateCategory.FREE_CANCELLATION,
            ),
        ],
        nights=2,
        profile=PROFILE,
        unit_filter=None,
    )
    selected = {o.rate_category: o.offer_index for o in offers if o.selected_for_category}
    # pokój (index 1) nie pasuje; niepełna cena (index 5) przegrywa z kompletną mimo niższej kwoty
    assert selected == {RateCategory.NON_REFUNDABLE: 0, RateCategory.FREE_CANCELLATION: 4}
    assert [o.offer_index for o in offers] == list(range(6))
    assert offers[1].matches_unit_filter is False


def test_partial_offer_selected_when_no_complete_price_in_category():
    offers = normalize_offers(
        [offer(70_000, comp(ComponentKind.CITY_TAX, ChargeBasis.PER_STAY, None))],
        nights=2,
        profile=PROFILE,
        unit_filter=None,
    )
    assert offers[0].selected_for_category
    assert offers[0].price_completeness is PriceCompleteness.PARTIAL


def test_each_stay_length_priced_separately():
    raw = offer(60_000, comp(ComponentKind.CITY_TAX, ChargeBasis.PER_PERSON_PER_NIGHT, 250))
    two = compute_price(raw, nights=2, guests=2)
    four = compute_price(raw, nights=4, guests=2)
    assert two.total_grosze == 61_000 and four.total_grosze == 62_000

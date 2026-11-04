"""Normalizacja ofert i kwalifikacja do porównań (ARCHITEKTURA.md §3).

Dla każdej kategorii taryfy wybieramy najtańszy pasujący cały apartament
(po filtrze lokalu). Oferta pasuje, gdy:
- nie jest jednoznacznie pokojem (is_entire_apartment != False; nieznany typ
  przyjmujemy jako pasujący, bo konkurentów wskazuje ręcznie właściciel),
- mieści wszystkich gości profilu (jeśli strona podaje pojemność),
- dotyczy liczby jednostek z profilu i waluty profilu,
- spełnia filtr lokalu: fragment nazwy (bez wielkości liter) albo `id:<identyfikator>`.
"""

from __future__ import annotations

from collections.abc import Sequence

from booking_monitor.config import Profile
from booking_monitor.models import Offer, PriceCompleteness, RateCategory, RawOffer
from booking_monitor.prices import compute_price


def unit_matches(offer: RawOffer, *, profile: Profile, unit_filter: str | None) -> bool:
    if offer.is_entire_apartment is False:
        return False
    if offer.max_occupancy is not None and offer.max_occupancy < profile.guests:
        return False
    if offer.units != profile.units:
        return False
    if offer.currency != profile.currency:
        return False
    if unit_filter:
        if unit_filter.startswith("id:"):
            return offer.unit_id == unit_filter[3:].strip()
        return unit_filter.casefold() in offer.unit_name.casefold()
    return True


def _selection_key(offer: Offer) -> tuple[int, int, int]:
    """Kompletne ceny przed niepełnymi; potem najniższa cena; remis → kolejność na stronie."""
    if (
        offer.price_completeness is PriceCompleteness.COMPLETE
        and offer.total_price_grosze is not None
    ):
        return (0, offer.total_price_grosze, offer.offer_index)
    return (1, offer.displayed_price_grosze, offer.offer_index)


def normalize_offers(
    raw_offers: Sequence[RawOffer],
    *,
    nights: int,
    profile: Profile,
    unit_filter: str | None,
) -> tuple[Offer, ...]:
    offers: list[Offer] = []
    for index, raw in enumerate(raw_offers):
        price = compute_price(raw, nights=nights, guests=profile.guests)
        offers.append(
            Offer(
                offer_index=index,
                unit_name=raw.unit_name,
                unit_id=raw.unit_id,
                is_entire_apartment=raw.is_entire_apartment,
                max_occupancy=raw.max_occupancy,
                units=raw.units,
                rate_name=raw.rate_name,
                rate_category=raw.rate_category,
                free_cancellation_until=raw.free_cancellation_until,
                meal_plan=raw.meal_plan,
                prepayment=raw.prepayment,
                discounts=raw.discounts,
                currency=raw.currency,
                displayed_price_grosze=raw.displayed_price_grosze,
                original_price_grosze=raw.original_price_grosze,
                total_price_grosze=price.total_grosze,
                avg_night_price_grosze=price.avg_night_grosze,
                price_completeness=price.completeness,
                deposit_grosze=price.deposit_grosze,
                matches_unit_filter=unit_matches(raw, profile=profile, unit_filter=unit_filter),
                components=price.components,
            )
        )

    selected: dict[RateCategory, Offer] = {}
    for offer in offers:
        if not offer.matches_unit_filter:
            continue
        best = selected.get(offer.rate_category)
        if best is None or _selection_key(offer) < _selection_key(best):
            selected[offer.rate_category] = offer
    chosen = {o.offer_index for o in selected.values()}
    return tuple(
        o.model_copy(update={"selected_for_category": o.offer_index in chosen}) for o in offers
    )

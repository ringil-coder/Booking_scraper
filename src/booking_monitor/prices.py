"""Pełny koszt pobytu (ARCHITEKTURA.md §3).

    pełna_cena     = kwota_widoczna + obowiązkowe_opłaty_jeszcze_niewliczone
    średnia_za_noc = pełna_cena / liczba_nocy

Kwoty w groszach (int), obliczenia na `Decimal`, zaokrąglenie do grosza „połówki w górę”.
Opłaty „za osobę” liczymy dla wszystkich gości profilu (dorośli + dzieci).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from booking_monitor.models import (
    ChargeBasis,
    ComponentKind,
    PriceCompleteness,
    PriceComponent,
    PriceComponentIn,
    RawOffer,
)


def round_grosze(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def stay_amount(
    component: PriceComponentIn, *, nights: int, guests: int, displayed_grosze: int
) -> int | None:
    """Kwota składnika za cały pobyt; None, gdy nie da się jej ustalić."""
    if component.basis is ChargeBasis.PERCENT:
        if component.rate_percent is None:
            return None
        return round_grosze(Decimal(displayed_grosze) * component.rate_percent / Decimal(100))
    if component.amount_grosze is None:
        return None
    multiplier = {
        ChargeBasis.PER_STAY: 1,
        ChargeBasis.PER_NIGHT: nights,
        ChargeBasis.PER_PERSON: guests,
        ChargeBasis.PER_PERSON_PER_NIGHT: guests * nights,
    }.get(component.basis)
    if multiplier is None:  # ChargeBasis.UNKNOWN
        return None
    return component.amount_grosze * multiplier


@dataclass(frozen=True)
class PriceResult:
    total_grosze: int | None
    avg_night_grosze: int | None
    completeness: PriceCompleteness
    deposit_grosze: int | None
    components: tuple[PriceComponent, ...]


def compute_price(offer: RawOffer, *, nights: int, guests: int) -> PriceResult:
    """Pełna cena pobytu dla konkretnej długości i profilu gości.

    - Składnik wliczony (`included`) nie jest doliczany drugi raz.
    - Opłata nieobowiązkowa nie wchodzi do ceny i nie wpływa na kompletność.
    - Kaucja zwrotna jest zapisywana osobno, poza ceną pobytu.
    - Nieznana kwota obowiązkowej, niewliczonej opłaty → total None i `partial`.
    """
    if nights < 1:
        raise ValueError("liczba nocy musi być dodatnia")
    displayed = offer.displayed_price_grosze
    complete = offer.fees_fully_disclosed
    extra = 0
    deposit: int | None = None
    deposit_unknown = False
    components: list[PriceComponent] = []

    for component in offer.components:
        amount = stay_amount(component, nights=nights, guests=guests, displayed_grosze=displayed)
        components.append(PriceComponent(**component.model_dump(), stay_amount_grosze=amount))
        if component.kind is ComponentKind.DEPOSIT:
            if amount is None:
                deposit_unknown = True
            else:
                deposit = (deposit or 0) + amount
            continue
        if not component.mandatory or component.included:
            continue
        if amount is None:
            complete = False
        else:
            extra += amount

    if deposit_unknown:
        deposit = None
    if not complete:
        return PriceResult(
            total_grosze=None,
            avg_night_grosze=None,
            completeness=PriceCompleteness.PARTIAL,
            deposit_grosze=deposit,
            components=tuple(components),
        )
    total = displayed + extra
    return PriceResult(
        total_grosze=total,
        avg_night_grosze=round_grosze(Decimal(total) / Decimal(nights)),
        completeness=PriceCompleteness.COMPLETE,
        deposit_grosze=deposit,
        components=tuple(components),
    )

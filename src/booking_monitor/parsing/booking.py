"""Parser wyrenderowanej strony oferty Booking.com (A§10, PLAN_DZIALANIA.md P2).

Czysta funkcja `parse_property_page(html) -> ParsedPage`. Rozstrzyga w kolejności:

1. blokada / CAPTCHA → `technical_reason=BLOCKED`;
2. tabela dostępności z czytelnymi wierszami → `AVAILABLE` + `RawOffer`;
3. jednoznaczny komunikat o niedostępności (także minimalna długość pobytu) → `UNAVAILABLE`;
4. cokolwiek innego (zmieniony układ, nieczytelne ceny) → `PARSE_ERROR` — nigdy `UNAVAILABLE`.

Kontekst (daty, goście, waluta) odczytujemy z wyrenderowanej strony, nie z adresu URL;
porównanie z zapytaniem należy do potoku obserwacji (`context_mismatch`).
Selektory i frazy: `parsing/selectors.py` (DO WERYFIKACJI W E1).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from booking_monitor.models import (
    Availability,
    ChargeBasis,
    ComponentKind,
    MealPlan,
    PageContext,
    ParsedPage,
    Prepayment,
    PriceComponentIn,
    RateCategory,
    RawOffer,
    TechnicalReason,
)
from booking_monitor.parsing import selectors as sel
from booking_monitor.parsing.text import (
    contains_any,
    detect_currency,
    first_int,
    fold,
    normalize_space,
    parse_date_text,
    parse_money,
    parse_percent,
    to_grosze,
)

# Podnosić przy każdej zmianie logiki parsera lub selektorów.
PARSER_VERSION = "booking-0.1.0"

_INVISIBLE_TAGS = frozenset({"script", "style", "noscript", "template", "head"})
_ROOM_TYPE_RE = re.compile(sel.ROOM_TYPE_PATTERN, re.IGNORECASE)
_ENTIRE_UNIT_RE = re.compile(sel.ENTIRE_UNIT_PATTERN, re.IGNORECASE)
# Podział listy opłat na pozycje: przecinek/średnik, po którym nie następuje cyfra
# (żeby nie rozcinać kwot typu „zł 2,50”), albo kropka kończąca zdanie.
_ITEM_SPLIT_RE = re.compile(r"[;,](?=\s*[^\d\s])|\.(?=\s+[A-ZĄĆĘŁŃÓŚŹŻ])")


class _RowError(ValueError):
    """Wiersz tabeli nie daje się jednoznacznie odczytać → PARSE_ERROR całej strony."""


# --------------------------------------------------------------------------- API


def parse_property_page(html: str, *, url: str | None = None) -> ParsedPage:
    """Interpretuje HTML strony oferty. `url` służy wyłącznie do komunikatów."""
    soup = BeautifulSoup(html or "", "lxml")
    context = extract_context(soup)
    common = {
        "parser_version": PARSER_VERSION,
        "property_name": _property_name(soup),
        "booking_property_id": _hotel_id(soup),
    }
    table = _first(soup, sel.AVAILABILITY_TABLE)

    block = detect_block(soup, has_table=table is not None)
    if block is not None:
        return ParsedPage(
            availability=Availability.UNKNOWN,
            technical_reason=TechnicalReason.BLOCKED,
            context=context,
            message=f"blokada lub CAPTCHA ({block})",
            **common,
        )

    sold_out = 0
    if table is not None:
        try:
            offers, sold_out = _parse_table(table, context.currency)
        except _RowError as exc:
            return _parse_error(context, f"nieczytelna tabela dostępności: {exc}", common)
        if offers:
            if context.currency is None:
                currencies = {offer.currency for offer in offers}
                if len(currencies) == 1:
                    context = context.model_copy(update={"currency": currencies.pop()})
            return ParsedPage(
                availability=Availability.AVAILABLE,
                context=context,
                offers=tuple(offers),
                **common,
            )

    message = unavailability_message(soup)
    if message is None and sold_out:
        message = "wszystkie warianty w tabeli są wyprzedane"
    if message is not None:
        return ParsedPage(
            availability=Availability.UNAVAILABLE,
            context=context,
            message=message,
            **common,
        )

    detail = "tabela bez wierszy ofert" if table is not None else "brak tabeli dostępności"
    where = f" ({url})" if url else ""
    return _parse_error(context, f"{detail} i brak komunikatu o niedostępności{where}", common)


def _parse_error(context: PageContext, message: str, common: dict) -> ParsedPage:
    return ParsedPage(
        availability=Availability.UNKNOWN,
        technical_reason=TechnicalReason.PARSE_ERROR,
        context=context,
        message=message,
        **common,
    )


# --------------------------------------------------------------------------- pomocnicze DOM


def _first(node: Tag, selectors: Iterable[str]) -> Tag | None:
    for selector in selectors:
        found = node.select_one(selector)
        if found is not None:
            return found
    return None


def _all(node: Tag, selectors: Iterable[str]) -> list[Tag]:
    for selector in selectors:
        found = node.select(selector)
        if found:
            return found
    return []


def visible_text(node: Tag) -> str:
    """Tekst widoczny dla użytkownika (bez skryptów, stylów i szablonów)."""
    parts: list[str] = []
    for piece in node.find_all(string=True):
        if not isinstance(piece, NavigableString):
            continue
        if any(parent.name in _INVISIBLE_TAGS for parent in piece.parents):
            continue
        parts.append(str(piece))
    return normalize_space(" ".join(parts))


def _text(node: Tag | None) -> str:
    return "" if node is None else visible_text(node)


def _field_value(node: Tag) -> str | None:
    if node.name == "select":
        option = node.select_one("option[selected]")
        value = option.get("value") if option is not None else None
    else:
        value = node.get("value")
    if isinstance(value, list):
        value = " ".join(value)
    value = normalize_space(value)
    return value or None


def _unique_field(soup: Tag, selectors: Iterable[str]) -> str | None:
    """Wartość pól formularza; sprzeczne wartości w kilku formularzach → None."""
    values = {
        value
        for selector in selectors
        for node in soup.select(selector)
        if (value := _field_value(node)) is not None
    }
    return values.pop() if len(values) == 1 else None


# --------------------------------------------------------------------------- blokada


def detect_block(soup: BeautifulSoup, *, has_table: bool) -> str | None:
    """Opis sygnału blokady/CAPTCHA albo None. Słabe sygnały tylko bez tabeli dostępności."""
    title = fold(soup.title.get_text(" ") if soup.title else "")
    if keyword := contains_any(title, sel.BLOCK_TITLE_KEYWORDS):
        return f"tytuł strony: {keyword}"
    for selector in sel.BLOCK_SELECTORS:
        if soup.select_one(selector) is not None:
            return f"element {selector}"
    for script in soup.find_all("script"):
        content = script.string or ""
        for marker in sel.BLOCK_SCRIPT_MARKERS:
            if marker in content:
                return f"skrypt wyzwania {marker}"
    if has_table:
        return None
    for selector in sel.BLOCK_WEAK_SELECTORS:
        if soup.select_one(selector) is not None:
            return f"element {selector}"
    if keyword := contains_any(visible_text(soup), sel.BLOCK_TEXT_KEYWORDS):
        return f"komunikat: {keyword}"
    return None


# --------------------------------------------------------------------------- niedostępność


def unavailability_message(soup: BeautifulSoup) -> str | None:
    """Jednoznaczny komunikat o braku dostępności dla sprawdzanych dat (A§9)."""
    keywords = sel.MIN_STAY_KEYWORDS + sel.UNAVAILABLE_KEYWORDS
    for selector in sel.UNAVAILABLE_SELECTORS:
        for node in soup.select(selector):
            text = _text(node)
            if not text:
                continue
            if selector in sel.UNAVAILABLE_DEDICATED or contains_any(text, keywords):
                return text
    # Awaryjnie: krótkie bloki tekstu bez zagnieżdżonych bloków.
    for node in soup.find_all(["p", "div", "span", "li", "h2", "h3"]):
        if node.find(["p", "div", "li"]) is not None:
            continue
        text = _text(node)
        if 0 < len(text) <= 400 and contains_any(text, keywords):
            return text
    return None


# --------------------------------------------------------------------------- kontekst


def extract_context(soup: BeautifulSoup) -> PageContext:
    checkin = (
        _iso_field(soup, sel.CHECKIN_INPUTS)
        or _split_date(soup, sel.CHECKIN_SPLIT_INPUTS)
        or _display_date(soup, sel.CHECKIN_DISPLAY)
    )
    checkout = (
        _iso_field(soup, sel.CHECKOUT_INPUTS)
        or _split_date(soup, sel.CHECKOUT_SPLIT_INPUTS)
        or _display_date(soup, sel.CHECKOUT_DISPLAY)
    )
    occupancy = _text(_first(soup, sel.OCCUPANCY_DISPLAY))
    return PageContext(
        checkin=checkin,
        checkout=checkout,
        adults=_int_field(soup, sel.ADULTS_INPUTS) or _pattern_int(occupancy, sel.ADULTS_PATTERN),
        children=_first_not_none(
            _int_field(soup, sel.CHILDREN_INPUTS), _pattern_int(occupancy, sel.CHILDREN_PATTERN)
        ),
        rooms=_int_field(soup, sel.ROOMS_INPUTS) or _pattern_int(occupancy, sel.ROOMS_PATTERN),
        currency=_currency(soup),
    )


def _first_not_none(*values: int | None) -> int | None:
    return next((v for v in values if v is not None), None)


def _iso_field(soup: BeautifulSoup, selectors: Iterable[str]) -> date | None:
    value = _unique_field(soup, selectors)
    if value is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    return parse_date_text(value)


def _split_date(soup: BeautifulSoup, selectors: tuple[str, str]) -> date | None:
    day = _unique_field(soup, [selectors[0]])
    year_month = _unique_field(soup, [selectors[1]])
    if not day or not year_month or not re.fullmatch(r"\d{4}-\d{1,2}", year_month):
        return None
    year, month = (int(part) for part in year_month.split("-"))
    return parse_date_text(f"{int(day):02d}.{month:02d}.{year}") if day.isdigit() else None


def _display_date(soup: BeautifulSoup, selectors: Iterable[str]) -> date | None:
    return parse_date_text(_text(_first(soup, selectors)))


def _int_field(soup: BeautifulSoup, selectors: Iterable[str]) -> int | None:
    value = _unique_field(soup, selectors)
    return int(value) if value is not None and value.isdigit() else None


def _pattern_int(text: str, pattern: str) -> int | None:
    match = re.search(pattern, fold(text))
    return int(match.group(1)) if match else None


def _currency(soup: BeautifulSoup) -> str | None:
    value = _unique_field(soup, sel.CURRENCY_INPUTS)
    if value and re.fullmatch(r"[A-Z]{3}", value):
        return value
    display = _text(_first(soup, sel.CURRENCY_DISPLAY))
    if display:
        if re.fullmatch(r"[A-Z]{3}", display):
            return display
        if code := detect_currency(display):
            return code
    scripts = " ".join(script.string or "" for script in soup.find_all("script"))
    for pattern in sel.CURRENCY_SCRIPT_PATTERNS:
        if match := re.search(pattern, scripts):
            return match.group(1)
    return None


def _property_name(soup: BeautifulSoup) -> str | None:
    return _text(_first(soup, sel.PROPERTY_NAME_SELECTORS)) or None


def _hotel_id(soup: BeautifulSoup) -> str | None:
    value = _unique_field(soup, sel.HOTEL_ID_INPUTS)
    if value and value.isdigit():
        return value
    for attribute in sel.HOTEL_ID_ATTRIBUTES:
        node = soup.find(attrs={attribute: True})
        if node is not None and str(node.get(attribute)).isdigit():
            return str(node.get(attribute))
    scripts = " ".join(script.string or "" for script in soup.find_all("script"))
    for pattern in sel.HOTEL_ID_SCRIPT_PATTERNS:
        if match := re.search(pattern, scripts):
            return match.group(1)
    return None


# --------------------------------------------------------------------------- tabela


@dataclass(frozen=True)
class _Room:
    name: str
    unit_id: str | None
    is_entire_apartment: bool | None


def classify_unit(name: str) -> bool | None:
    """Cały lokal (True), pokój/łóżko (False) albo nie wiadomo (None)."""
    folded = fold(name)
    if _ROOM_TYPE_RE.search(folded):
        return False
    if _ENTIRE_UNIT_RE.search(folded):
        return True
    return None


def _parse_room(cell: Tag) -> _Room:
    name_node = _first(cell, sel.ROOM_NAME)
    name = _text(name_node) or _text(cell)
    if not name:
        raise _RowError("brak nazwy lokalu")
    unit_id = None
    for node in (name_node, cell):
        if node is None:
            continue
        for attribute in sel.ROOM_ID_ATTRIBUTES:
            if node.get(attribute):
                unit_id = str(node.get(attribute))
                break
        if unit_id:
            break
    return _Room(name=name, unit_id=unit_id, is_entire_apartment=classify_unit(name))


def _is_sold_out(row: Tag) -> bool:
    classes = set(row.get("class") or [])
    return bool(classes.intersection(sel.SOLD_OUT_ROW_CLASSES))


def _parse_table(table: Tag, default_currency: str | None) -> tuple[list[RawOffer], int]:
    rows = _all(table, sel.TABLE_ROWS)
    offers: list[RawOffer] = []
    sold_out = 0
    room: _Room | None = None
    for index, row in enumerate(rows, start=1):
        room_cell = _first(row, sel.ROOMTYPE_CELL)
        if room_cell is not None:
            room = _parse_room(room_cell)
        if _is_sold_out(row):
            sold_out += 1
            continue
        if room is None:
            raise _RowError(f"wiersz {index} bez typu lokalu")
        offers.append(_parse_row(row, index, room, default_currency))
    return offers, sold_out


def _parse_row(row: Tag, index: int, room: _Room, default_currency: str | None) -> RawOffer:
    price_cell = _first(row, sel.PRICE_CELL) or row
    price = parse_money(_text(_first(price_cell, sel.PRICE_VALUE)), default_currency)
    if price is None:
        raise _RowError(f"wiersz {index}: brak czytelnej ceny")
    if price.currency is None:
        raise _RowError(f"wiersz {index}: nieznana waluta ceny")
    displayed = to_grosze(price.amount)

    original = parse_money(_text(_first(price_cell, sel.PRICE_ORIGINAL)), price.currency)
    original_grosze = to_grosze(original.amount) if original is not None else None
    if original_grosze is not None and original_grosze <= displayed:
        original_grosze = None

    block_id = str(row.get("data-block-id") or "")
    unit_id = room.unit_id or (block_id.split("_")[0] if block_id else None)

    conditions = _conditions(row)
    rate_category, cancellation_until = _rate_category(conditions)
    components, disclosed = _price_components(price_cell, price.currency)

    return RawOffer(
        unit_name=room.name,
        unit_id=unit_id or None,
        is_entire_apartment=room.is_entire_apartment,
        max_occupancy=_max_occupancy(row),
        rate_name=_text(_first(row, sel.RATE_NAME)) or None,
        currency=price.currency,
        displayed_price_grosze=displayed,
        original_price_grosze=original_grosze,
        discounts=_discounts(price_cell),
        rate_category=rate_category,
        free_cancellation_until=cancellation_until,
        meal_plan=_meal_plan(conditions),
        prepayment=_prepayment(conditions),
        components=tuple(components),
        fees_fully_disclosed=disclosed,
    )


def _max_occupancy(row: Tag) -> int | None:
    cell = _first(row, sel.OCCUPANCY_CELL)
    if cell is None:
        return None
    value = first_int(_text(_first(cell, sel.OCCUPANCY_TEXT)))
    if value is not None:
        return value
    icons = _all(cell, sel.OCCUPANCY_ICONS)
    return len(icons) or None


def _discounts(price_cell: Tag) -> tuple[str, ...]:
    texts = [_text(node) for node in _all(price_cell, sel.DISCOUNT_BADGES)]
    return tuple(dict.fromkeys(t for t in texts if t))


def _conditions(row: Tag) -> list[str]:
    cell = _first(row, sel.CONDITIONS_CELL)
    if cell is None:
        return []
    items = [_text(node) for node in _all(cell, sel.CONDITION_ITEMS)]
    items = [item for item in items if item]
    return items or [t for t in [_text(cell)] if t]


def _rate_category(conditions: list[str]) -> tuple[RateCategory, date | None]:
    for item in conditions:
        if contains_any(item, sel.NON_REFUNDABLE_KEYWORDS):
            return RateCategory.NON_REFUNDABLE, None
    for item in conditions:
        if contains_any(item, sel.FREE_CANCELLATION_KEYWORDS):
            return RateCategory.FREE_CANCELLATION, parse_date_text(item)
    for item in conditions:
        if contains_any(item, sel.OTHER_CANCELLATION_KEYWORDS):
            return RateCategory.OTHER, None
    return RateCategory.UNKNOWN, None


def _meal_plan(conditions: list[str]) -> MealPlan:
    text = " | ".join(conditions)
    for plan, keywords in (
        (MealPlan.ALL_INCLUSIVE, sel.ALL_INCLUSIVE_KEYWORDS),
        (MealPlan.FULL_BOARD, sel.FULL_BOARD_KEYWORDS),
        (MealPlan.HALF_BOARD, sel.HALF_BOARD_KEYWORDS),
        (MealPlan.BREAKFAST, sel.BREAKFAST_INCLUDED_KEYWORDS),
        (MealPlan.ROOM_ONLY, sel.ROOM_ONLY_KEYWORDS),
    ):
        if contains_any(text, keywords):
            return plan
    return MealPlan.UNKNOWN


def _prepayment(conditions: list[str]) -> Prepayment:
    text = " | ".join(conditions)
    if contains_any(text, sel.NO_PREPAYMENT_KEYWORDS):
        return Prepayment.NO_PREPAYMENT
    if contains_any(text, sel.PREPAYMENT_KEYWORDS):
        return Prepayment.PREPAYMENT_REQUIRED
    return Prepayment.UNKNOWN


# --------------------------------------------------------------------------- podatki i opłaty


def _price_components(price_cell: Tag, currency: str) -> tuple[list[PriceComponentIn], bool]:
    """Składniki ceny i informacja, czy wszystkie obowiązkowe opłaty mają znaną kwotę.

    Jeśli pod ceną podano łączną kwotę „+ zł X podatków i opłat”, jest ona jedynym
    niewliczonym składnikiem (pozycje szczegółowe opisują tę samą sumę — nie
    doliczamy ich drugi raz, A§3). Kaucję zwrotną zapisujemy zawsze, osobno.
    """
    components: list[PriceComponentIn] = []
    disclosed = True
    aggregate = False

    for node in _all(price_cell, sel.TAXES_UNDER_PRICE):
        text = _text(node)
        if contains_any(text, sel.UNDISCLOSED_FEES_KEYWORDS):
            disclosed = False
            continue
        if contains_any(text, sel.TAXES_INCLUDED_KEYWORDS):
            continue
        if contains_any(text, sel.TAXES_EXCLUDED_AGGREGATE_KEYWORDS):
            money = parse_money(text, currency)
            if money is None:
                disclosed = False
                continue
            aggregate = True
            components.append(
                PriceComponentIn(
                    name="Podatki i opłaty (łącznie)",
                    kind=ComponentKind.OTHER_TAX,
                    basis=ChargeBasis.PER_STAY,
                    amount_grosze=to_grosze(money.amount),
                    included=False,
                )
            )

    for node in _all(price_cell, sel.TAX_DETAILS):
        text = _text(node)
        if contains_any(text, sel.UNDISCLOSED_FEES_KEYWORDS):
            disclosed = False
        for included, item in _tax_items(text):
            component = _parse_component(item, included, currency)
            if component is None:
                continue
            if component.kind is ComponentKind.DEPOSIT:
                components.append(component)
            elif included is None:
                continue  # pozycja bez informacji, czy jest wliczona — tylko informacyjna
            elif not component.included and aggregate:
                continue  # zawarta w łącznej kwocie spod ceny
            else:
                components.append(component)
    return components, disclosed


def _tax_items(text: str) -> list[tuple[bool | None, str]]:
    """Dzieli tekst na pozycje z flagą: wliczona (True), niewliczona (False), brak (None)."""
    folded = fold(text)
    markers: list[tuple[int, int, bool]] = []
    for flag, keywords in ((False, sel.EXCLUDED_LIST_MARKERS), (True, sel.INCLUDED_LIST_MARKERS)):
        for keyword in keywords:
            start = 0
            while (index := folded.find(keyword, start)) >= 0:
                markers.append((index, index + len(keyword), flag))
                start = index + len(keyword)
    # „nie zawiera:” zawiera w sobie „zawiera:” — usuń znaczniki nakładające się.
    markers.sort(key=lambda m: (m[0], -(m[1] - m[0])))
    filtered: list[tuple[int, int, bool]] = []
    for marker in markers:
        if filtered and marker[0] < filtered[-1][1]:
            continue
        filtered.append(marker)

    normalized = normalize_space(text)
    segments: list[tuple[bool | None, str]] = []
    if not filtered:
        segments.append((None, normalized))
    else:
        if filtered[0][0] > 0:
            segments.append((None, normalized[: filtered[0][0]]))
        for position, marker in enumerate(filtered):
            end = filtered[position + 1][0] if position + 1 < len(filtered) else len(normalized)
            segments.append((marker[2], normalized[marker[1] : end]))

    items: list[tuple[bool | None, str]] = []
    for flag, segment in segments:
        for item in _ITEM_SPLIT_RE.split(segment):
            item = item.strip(" .;,")
            if item:
                items.append((flag, item))
    return items


def _component_kind(text: str) -> ComponentKind:
    folded = fold(text)
    for kind, keywords in sel.COMPONENT_KIND_KEYWORDS:
        if any(keyword in folded for keyword in keywords):
            return ComponentKind(kind)
    return ComponentKind.OTHER_FEE


def _basis(text: str) -> ChargeBasis:
    for basis, keywords in (
        (ChargeBasis.PER_PERSON_PER_NIGHT, sel.BASIS_PER_PERSON_PER_NIGHT),
        (ChargeBasis.PER_NIGHT, sel.BASIS_PER_NIGHT),
        (ChargeBasis.PER_PERSON, sel.BASIS_PER_PERSON),
        (ChargeBasis.PER_STAY, sel.BASIS_PER_STAY),
    ):
        if contains_any(text, keywords):
            return basis
    return ChargeBasis.UNKNOWN


def _parse_component(text: str, included: bool | None, currency: str) -> PriceComponentIn | None:
    kind = _component_kind(text)
    name = normalize_space(text)[:120]
    if kind is ComponentKind.DEPOSIT:
        money = parse_money(text, currency)
        return PriceComponentIn(
            name=name,
            kind=kind,
            basis=ChargeBasis.PER_STAY,
            amount_grosze=to_grosze(money.amount) if money else None,
            mandatory=True,
            included=False,
        )
    has_currency = detect_currency(text) is not None
    percent = parse_percent(text)
    if percent is not None and not has_currency:
        return PriceComponentIn(
            name=name,
            kind=kind,
            basis=ChargeBasis.PERCENT,
            rate_percent=percent,
            included=bool(included),
        )
    money = parse_money(text, currency) if has_currency else None
    if money is None and kind is ComponentKind.OTHER_FEE and not contains_any(text, ("opłat",)):
        return None  # tekst niebędący opłatą (np. luźna uwaga)
    return PriceComponentIn(
        name=name,
        kind=kind,
        basis=_basis(text) if money is not None else ChargeBasis.UNKNOWN,
        amount_grosze=to_grosze(money.amount) if money is not None else None,
        included=bool(included),
    )

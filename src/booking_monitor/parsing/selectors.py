"""Selektory CSS i słowa kluczowe strony oferty Booking.com.

!!! DO WERYFIKACJI W E1 !!!
Wszystkie selektory i frazy poniżej opisują strukturę strony na podstawie wiedzy
o jej dotychczasowym układzie i SYNTETYCZNYCH próbek z `tests/fixtures/booking/`.
Nie zostały sprawdzone na prawdziwych stronach. Pilotaż (PLAN_DZIALANIA.md, E1)
musi je potwierdzić lub poprawić; każda zmiana tego pliku wymaga podniesienia
`PARSER_VERSION` w `parsing/booking.py`.

Każda lista to alternatywy sprawdzane po kolei — pierwsze dopasowanie wygrywa.
Słowa kluczowe są porównywane z tekstem po `text.fold()` (małe litery, zwykłe spacje).
"""

from __future__ import annotations

# --------------------------------------------------------------------------- blokada

# Silne sygnały blokady / CAPTCHA — rozstrzygają niezależnie od reszty strony.
BLOCK_SELECTORS: tuple[str, ...] = (
    "#captcha-container",
    "#challenge-container",
    "#challenge-form",
)
# Słabe sygnały — tylko na stronie bez tabeli dostępności i bez nagłówka obiektu.
# Pilotaż E1: zwykła strona oferty ładuje integrację AWS WAF (skrypt *.awswaf.com,
# `window.awsWafCookieDomainList`), więc te znaczniki same nie dowodzą blokady.
# Prawdziwa strona wyzwania (HTTP 202) ma `#challenge-container` — sygnał silny.
BLOCK_WEAK_SELECTORS: tuple[str, ...] = (
    "iframe[src*='captcha']",
    "script[src*='awswaf']",
    "script[src*='captcha']",
)
BLOCK_SCRIPT_MARKERS: tuple[str, ...] = ("gokuProps", "AwsWafIntegration", "awsWafCookieDomainList")
BLOCK_TITLE_KEYWORDS: tuple[str, ...] = (
    "access denied",
    "odmowa dostępu",
    "attention required",
    "just a moment",
    "request blocked",
)
# Słabe sygnały — brane pod uwagę tylko, gdy strona nie ma tabeli dostępności.
BLOCK_TEXT_KEYWORDS: tuple[str, ...] = (
    "captcha",
    "jesteś człowiekiem",
    "nie jesteś robotem",
    "verify you are human",
    "are you a robot",
    "unusual traffic",
    "nietypowy ruch",
)

# --------------------------------------------------------------------------- kontekst

PROPERTY_NAME_SELECTORS: tuple[str, ...] = (
    "#hp_hotel_name",
    "[data-testid='property-name']",
    "h2.pp-header__title",
    "[data-capla-component-boundary*='PropertyHeaderName'] h2",
)
HOTEL_ID_INPUTS: tuple[str, ...] = ("input[name='hotel_id']", "input[name='b_hotel_id']")
HOTEL_ID_ATTRIBUTES: tuple[str, ...] = ("data-hotel-id", "data-hotelid")
HOTEL_ID_SCRIPT_PATTERNS: tuple[str, ...] = (
    r"b_hotel_id\s*[:=]\s*['\"]?(\d+)",
    r"[\"']hotel_id[\"']\s*:\s*['\"]?(\d+)",
)

CHECKIN_INPUTS: tuple[str, ...] = ("input[name='checkin']",)
CHECKOUT_INPUTS: tuple[str, ...] = ("input[name='checkout']",)
# Starszy formularz: dzień + rok-miesiąc w osobnych polach.
CHECKIN_SPLIT_INPUTS: tuple[str, str] = (
    "input[name='checkin_monthday']",
    "input[name='checkin_year_month']",
)
CHECKOUT_SPLIT_INPUTS: tuple[str, str] = (
    "input[name='checkout_monthday']",
    "input[name='checkout_year_month']",
)
CHECKIN_DISPLAY: tuple[str, ...] = (
    "[data-testid='date-display-field-start']",
    ".sb-date-field__display[data-mode='checkin']",
)
CHECKOUT_DISPLAY: tuple[str, ...] = (
    "[data-testid='date-display-field-end']",
    ".sb-date-field__display[data-mode='checkout']",
)
ADULTS_INPUTS: tuple[str, ...] = ("input[name='group_adults']", "select[name='group_adults']")
CHILDREN_INPUTS: tuple[str, ...] = (
    "input[name='group_children']",
    "select[name='group_children']",
)
ROOMS_INPUTS: tuple[str, ...] = ("input[name='no_rooms']", "select[name='no_rooms']")
OCCUPANCY_DISPLAY: tuple[str, ...] = (
    "[data-testid='occupancy-config']",
    ".xp__guests__count",
)
# Wzorce w tekście „2 dorosłych · 0 dzieci · 1 pokój”.
ADULTS_PATTERN = r"(\d+)\s*(?:dorosł\w*|adults?)"
CHILDREN_PATTERN = r"(\d+)\s*(?:dzieci|dziecko|dzieck\w*|child\w*)"
ROOMS_PATTERN = r"(\d+)\s*(?:pok[oó]\w*|rooms?)"

CURRENCY_INPUTS: tuple[str, ...] = ("input[name='selected_currency']",)
CURRENCY_DISPLAY: tuple[str, ...] = (
    "[data-testid='header-currency-picker-trigger']",
    ".bui-header__currency",
)
CURRENCY_SCRIPT_PATTERNS: tuple[str, ...] = (
    r"b_selected_currency\s*[:=]\s*['\"]([A-Z]{3})['\"]",
    r"[\"']selected_currency[\"']\s*:\s*['\"]([A-Z]{3})['\"]",
)

# --------------------------------------------------------------------------- tabela

AVAILABILITY_TABLE: tuple[str, ...] = (
    "table#hprt-table",
    "table.hprt-table",
    "[data-testid='availability-table']",
)
TABLE_ROWS: tuple[str, ...] = ("tr.js-rt-block-row", "tr[data-block-id]")
SOLD_OUT_ROW_CLASSES: tuple[str, ...] = ("hprt-sold-out", "hprt-table-row--sold-out")

ROOMTYPE_CELL: tuple[str, ...] = (".hprt-table-cell-roomtype", "[data-testid='room-type-cell']")
ROOM_NAME: tuple[str, ...] = (
    ".hprt-roomtype-icon-link",
    "a.hprt-roomtype-link",
    "[data-testid='room-name']",
)
ROOM_ID_ATTRIBUTES: tuple[str, ...] = ("data-room-id",)

OCCUPANCY_CELL: tuple[str, ...] = (".hprt-table-cell-occupancy", "[data-testid='occupancy-cell']")
OCCUPANCY_TEXT: tuple[str, ...] = (".bui-u-sr-only", ".c-occupancy-icons__sr-text")
OCCUPANCY_ICONS: tuple[str, ...] = (".c-occupancy-icons__adults i", "i.bicon-occupancy")

PRICE_CELL: tuple[str, ...] = (".hprt-table-cell-price", "[data-testid='price-cell']")
PRICE_VALUE: tuple[str, ...] = (
    ".bui-price-display__value",
    ".prco-valign-middle-helper",
    "[data-testid='price-and-discounted-price']",
)
PRICE_ORIGINAL: tuple[str, ...] = (
    ".bui-price-display__original",
    ".hprt-price-strikethrough",
    "[data-testid='strikethrough-price']",
)
TAXES_UNDER_PRICE: tuple[str, ...] = (
    ".prd-taxes-and-fees-under-price",
    ".prco-taxes-and-fees-under-price",
    "[data-testid='taxes-and-charges']",
)
TAX_DETAILS: tuple[str, ...] = (
    ".hprt-taxinfo",
    ".hptr-taxinfo-block",
    "[data-testid='tax-details']",
)
DISCOUNT_BADGES: tuple[str, ...] = (
    ".hprt-price-block .bui-badge",
    "[data-testid='discount-badge']",
    ".discount-badge",
)

CONDITIONS_CELL: tuple[str, ...] = (
    ".hprt-table-cell-conditions",
    "[data-testid='conditions-cell']",
)
CONDITION_ITEMS: tuple[str, ...] = ("li", ".bui-list__item")
RATE_NAME: tuple[str, ...] = (".hprt-rate-name", "[data-testid='rate-name']")

# --------------------------------------------------------------------------- niedostępność

# Kontenery przeznaczone wyłącznie na komunikat o braku dostępności — wystarczy ich treść.
UNAVAILABLE_DEDICATED: tuple[str, ...] = (
    "[data-testid='no-availability-message']",
    "#no_availability_msg",
)
# Ogólne kontenery komunikatów — liczą się tylko z frazą o niedostępności.
UNAVAILABLE_SELECTORS: tuple[str, ...] = (
    *UNAVAILABLE_DEDICATED,
    ".hp-no-availability",
    "#hp_availability_style_changes .bui-alert",
    ".bui-alert--error",
)
UNAVAILABLE_KEYWORDS: tuple[str, ...] = (
    "brak dostępnych miejsc",
    "brak wolnych miejsc",
    "nie ma wolnych",
    "nie mamy wolnych",
    "nie mamy dostępnych",
    "obiekt nie ma dostępności",
    "nie ma dostępności",
    "brak dostępności",
    "no availability",
    "not available on our site",
    "sold out",
)
MIN_STAY_KEYWORDS: tuple[str, ...] = (
    "minimalna długość pobytu",
    "minimalny pobyt",
    "minimalnego pobytu",
    "pobyt musi trwać co najmniej",
    "minimum stay",
    "minimum length of stay",
)

# --------------------------------------------------------------------------- warunki taryf

NON_REFUNDABLE_KEYWORDS: tuple[str, ...] = ("bezzwrotn", "non-refundable", "nonrefundable")
FREE_CANCELLATION_KEYWORDS: tuple[str, ...] = (
    "bezpłatne odwołanie",
    "bezpłatna anulacja",
    "bezpłatne anulowanie",
    "darmowe odwołanie",
    "free cancellation",
)
OTHER_CANCELLATION_KEYWORDS: tuple[str, ...] = (
    "odwołan",
    "anulowan",
    "anulacj",
    "częściowo zwrotn",
    "cancellation",
)

NO_PREPAYMENT_KEYWORDS: tuple[str, ...] = (
    "nie wymaga przedpłaty",
    "bez przedpłaty",
    "przedpłata nie jest wymagana",
    "zapłać w obiekcie",
    "no prepayment",
)
PREPAYMENT_KEYWORDS: tuple[str, ...] = (
    "przedpłat",
    "zapłać z wyprzedzeniem",
    "płatność z góry",
    "prepayment",
    "pay in advance",
)

# Kolejność ma znaczenie: bardziej szczegółowe plany przed śniadaniem.
ALL_INCLUSIVE_KEYWORDS: tuple[str, ...] = ("all inclusive", "all-inclusive", "wszystko w cenie")
FULL_BOARD_KEYWORDS: tuple[str, ...] = ("pełne wyżywienie", "full board")
HALF_BOARD_KEYWORDS: tuple[str, ...] = (
    "śniadanie i kolacja",
    "śniadanie i obiad",
    "półpensja",
    "half board",
)
BREAKFAST_INCLUDED_KEYWORDS: tuple[str, ...] = (
    "śniadanie wliczone",
    "śniadanie w cenie",
    "ze śniadaniem",
    "breakfast included",
)
# Płatne śniadanie (opcjonalne) albo wprost brak wyżywienia → sam nocleg.
ROOM_ONLY_KEYWORDS: tuple[str, ...] = (
    "śniadanie za",
    "śniadanie dostępne za",
    "bez wyżywienia",
    "tylko nocleg",
    "room only",
    "breakfast for",
)

DISCOUNT_KEYWORDS: tuple[str, ...] = ("okazja", "zniżk", "rabat", "oferta", "promocj", "deal")

# --------------------------------------------------------------------------- podatki i opłaty

TAXES_INCLUDED_KEYWORDS: tuple[str, ...] = (
    "zawiera podatki i opłaty",
    "podatki i opłaty wliczone",
    "w tym podatki i opłaty",
    "includes taxes and charges",
)
TAXES_EXCLUDED_AGGREGATE_KEYWORDS: tuple[str, ...] = ("podatków i opłat", "taxes and charges")
# Komunikaty o dodatkowych opłatach bez kwoty → cena nie jest pełna.
UNDISCLOSED_FEES_KEYWORDS: tuple[str, ...] = (
    "mogą obowiązywać dodatkowe",
    "mogą zostać naliczone dodatkowe",
    "dodatkowe opłaty mogą",
    "dodatkowe podatki i opłaty mogą",
    "additional charges may apply",
)
INCLUDED_LIST_MARKERS: tuple[str, ...] = ("zawiera:", "w cenie:", "includes:")
EXCLUDED_LIST_MARKERS: tuple[str, ...] = ("nie zawiera:", "niewliczone:", "excludes:")

COMPONENT_KIND_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deposit", ("kaucj", "depozyt", "deposit")),
    ("vat", ("vat",)),
    ("city_tax", ("miejscow", "klimatyczn", "turystyczn", "uzdrowiskow", "city tax")),
    ("cleaning", ("sprzątan", "cleaning")),
    ("service", ("serwis", "service")),
    ("other_tax", ("podat", "tax")),
    ("other_fee", ("opłat", "fee", "charge")),
)
BASIS_PER_PERSON_PER_NIGHT: tuple[str, ...] = (
    "za osobę za noc",
    "za osobę/noc",
    "za osobę na noc",
    "per person per night",
)
BASIS_PER_NIGHT: tuple[str, ...] = ("za noc", "per night")
BASIS_PER_PERSON: tuple[str, ...] = ("za osobę", "per person")
BASIS_PER_STAY: tuple[str, ...] = ("za pobyt", "per stay", "jednorazow")

# --------------------------------------------------------------------------- przeglądarka

# Baner zgód: najpierw odrzucenie opcjonalnych plików cookie, potem zamknięcie banera.
# Nigdy nie klikamy „Zaakceptuj wszystkie”.
COOKIE_REJECT_SELECTORS: tuple[str, ...] = (
    "#onetrust-reject-all-handler",
    "button:has-text('Odrzuć')",
    "button:has-text('Odrzuć wszystkie')",
    "button:has-text('Decline')",
)
COOKIE_CLOSE_SELECTORS: tuple[str, ...] = (
    "#onetrust-banner-sdk .onetrust-close-btn-handler",
    "#onetrust-banner-sdk button[aria-label='Zamknij']",
)

# --------------------------------------------------------------------------- typ lokalu

# Sprawdzane najpierw: jednoznaczne pokoje / łóżka w sali → nie cały apartament.
ROOM_TYPE_PATTERN = r"\b(?:pokój|pokoj|room|dormitor\w*|hostel)\b|\b(?:łóżko|bed)\s+(?:w|in)\b"
ENTIRE_UNIT_PATTERN = (
    r"\b(apartament\w*|apartment\w*|studio|dom|domek|willa|villa|loft|"
    r"mieszkanie|cały|całe|holiday home|house)\b"
)

"""Pomocnicze funkcje tekstowe parsera: kwoty, daty po polsku, procenty, liczby.

Wszystkie funkcje są czyste i nie zależą od struktury strony Booking.
Kwoty liczymy wyłącznie na `Decimal`; wynik w groszach to `int`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Spacje używane w formatach liczb: NBSP, wąska NBSP, cienka spacja, figure space.
_SPACE_CHARS = "     "
_SPACE_TABLE = str.maketrans(dict.fromkeys(_SPACE_CHARS, " "))
_INVISIBLE = re.compile("[​‌‍⁠﻿]")
_WHITESPACE = re.compile(r"\s+")

# Oznaczenia walut → kod ISO 4217. Kolejność: dłuższe tokeny najpierw.
CURRENCY_TOKENS: tuple[tuple[str, str], ...] = (
    ("US$", "USD"),
    ("PLN", "PLN"),
    ("EUR", "EUR"),
    ("USD", "USD"),
    ("GBP", "GBP"),
    ("CZK", "CZK"),
    ("CHF", "CHF"),
    ("zł", "PLN"),
    ("Kč", "CZK"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("$", "USD"),
)

# Liczba z grupami tysięcy (spacja, kropka, przecinek) albo bez, z 1–2 miejscami po przecinku.
_NUMBER_RE = re.compile(
    r"(?<![\d.,])(?:\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?!\d)"
)
_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def normalize_space(text: str | None) -> str:
    """Ujednolica spacje (NBSP, cienkie) i usuwa znaki niewidoczne."""
    if not text:
        return ""
    cleaned = _INVISIBLE.sub("", text.translate(_SPACE_TABLE))
    return _WHITESPACE.sub(" ", cleaned).strip()


def fold(text: str | None) -> str:
    """Tekst do porównań słów kluczowych: znormalizowane spacje, małe litery."""
    return normalize_space(text).lower()


def contains_any(text: str, keywords: tuple[str, ...]) -> str | None:
    """Zwraca pierwsze słowo kluczowe zawarte w `text` (po `fold`) albo None."""
    folded = fold(text)
    for keyword in keywords:
        if keyword in folded:
            return keyword
    return None


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str | None


def detect_currency(text: str) -> str | None:
    normalized = normalize_space(text)
    for token, code in CURRENCY_TOKENS:
        if token.isalpha() and token.isascii():
            if re.search(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])", normalized):
                return code
        elif token in normalized:
            return code
    return None


def parse_decimal(number: str) -> Decimal | None:
    """Liczba w formacie polskim lub angielskim: '1 234,56', '1,234.56', '1.234', '1234'."""
    raw = normalize_space(number).replace(" ", "")
    if not raw or not re.fullmatch(r"\d[\d,.]*", raw):
        return None
    has_comma, has_dot = "," in raw, "." in raw
    if has_comma and has_dot:
        decimal_sep = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        raw = raw.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        parts = raw.split(sep)
        if len(parts) > 2 or len(parts[-1]) == 3:
            # Separator tysięcy: '1,234', '1.234.567'.
            if any(len(p) != 3 for p in parts[1:]):
                return None
            raw = "".join(parts)
        else:
            raw = parts[0] + "." + parts[1]
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_money(text: str | None, default_currency: str | None = None) -> Money | None:
    """Pierwsza kwota w tekście, np. 'zł 1 234', '1 234,56 zł', 'PLN 1,234', '€ 245'."""
    normalized = normalize_space(text)
    if not normalized:
        return None
    matches = list(_NUMBER_RE.finditer(normalized))
    if not matches:
        return None
    currency = detect_currency(normalized)
    match = matches[0]
    if currency is not None and len(matches) > 1:
        # Kilka liczb: wybierz najbliższą oznaczeniu waluty ('3 noce: zł 1 020' → 1 020).
        position = _currency_position(normalized)
        match = min(matches, key=lambda m: min(abs(m.start() - position), abs(m.end() - position)))
    amount = parse_decimal(match.group(0))
    if amount is None:
        return None
    return Money(amount=amount, currency=currency or default_currency)


def _currency_position(text: str) -> int:
    for token, _code in CURRENCY_TOKENS:
        index = text.find(token)
        if index >= 0:
            return index
    return 0


def to_grosze(amount: Decimal) -> int:
    """Kwota → grosze (zaokrąglenie połówkowe w górę)."""
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money_to_grosze(text: str | None) -> int | None:
    money = parse_money(text)
    return None if money is None else to_grosze(money.amount)


def parse_percent(text: str | None) -> Decimal | None:
    match = _PERCENT_RE.search(normalize_space(text))
    if not match:
        return None
    return Decimal(match.group(1).replace(",", "."))


def first_int(text: str | None) -> int | None:
    match = re.search(r"\d+", normalize_space(text))
    return int(match.group(0)) if match else None


# --------------------------------------------------------------------------- daty

# Rdzenie nazw miesięcy (mianownik, dopełniacz, skróty; polskie i angielskie).
_MONTH_STEMS: tuple[tuple[str, int], ...] = (
    ("stycz", 1),
    ("sty", 1),
    ("jan", 1),
    ("lut", 2),
    ("feb", 2),
    ("marz", 3),
    ("marc", 3),
    ("mar", 3),
    ("kwie", 4),
    ("kwi", 4),
    ("apr", 4),
    ("maj", 5),
    ("may", 5),
    ("czerw", 6),
    ("cze", 6),
    ("jun", 6),
    ("lip", 7),
    ("jul", 7),
    ("sierp", 8),
    ("sie", 8),
    ("aug", 8),
    ("wrze", 9),
    ("wrz", 9),
    ("sep", 9),
    ("paźdz", 10),
    ("paź", 10),
    ("paz", 10),
    ("oct", 10),
    ("listop", 11),
    ("lis", 11),
    ("nov", 11),
    ("grud", 12),
    ("gru", 12),
    ("dec", 12),
)

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DOTTED_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
_WORD_DATE_RE = re.compile(r"\b(\d{1,2})\s+([a-ząćęłńóśźż]{3,}\.?)\s*(\d{4})?\b")


def month_from_word(word: str) -> int | None:
    folded = fold(word).rstrip(".")
    for stem, month in _MONTH_STEMS:
        if folded.startswith(stem):
            return month
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date_text(text: str | None, *, default_year: int | None = None) -> date | None:
    """Data z tekstu: '2026-11-20', '20.11.2026', '2 grudnia 2026', 'pt., 20 lis 2026'.

    Gdy rok nie jest podany, używa `default_year` (jeśli brak — zwraca None).
    """
    normalized = normalize_space(text)
    if not normalized:
        return None
    if match := _ISO_DATE_RE.search(normalized):
        return _safe_date(int(match[1]), int(match[2]), int(match[3]))
    if match := _DOTTED_DATE_RE.search(normalized):
        return _safe_date(int(match[3]), int(match[2]), int(match[1]))
    for match in _WORD_DATE_RE.finditer(normalized.lower()):
        month = month_from_word(match[2])
        if month is None:
            continue
        year = int(match[3]) if match[3] else default_year
        if year is None:
            return None
        return _safe_date(year, month, int(match[1]))
    return None

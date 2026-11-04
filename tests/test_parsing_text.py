from datetime import date
from decimal import Decimal

import pytest

from booking_monitor.parsing.text import (
    detect_currency,
    first_int,
    fold,
    money_to_grosze,
    normalize_space,
    parse_date_text,
    parse_decimal,
    parse_money,
    parse_percent,
    to_grosze,
)


@pytest.mark.parametrize(
    ("text", "grosze", "currency"),
    [
        ("zł 1 234", 123400, "PLN"),
        ("1 234,56 zł", 123456, "PLN"),
        ("zł 1 020", 102000, "PLN"),
        ("zł 1 020", 102000, "PLN"),
        ("PLN 1,234", 123400, "PLN"),
        ("PLN 1,234.50", 123450, "PLN"),
        ("1.234,56 zł", 123456, "PLN"),
        ("€ 245", 24500, "EUR"),
        ("EUR 245,90", 24590, "EUR"),
        ("US$ 99", 9900, "USD"),
        ("+ zł 96 podatków i opłat", 9600, "PLN"),
        ("zł 2,50 za osobę za noc", 250, "PLN"),
        ("3 noce: zł 1 020", 102000, "PLN"),
        ("1234", 123400, None),
    ],
)
def test_parse_money(text: str, grosze: int, currency: str | None) -> None:
    money = parse_money(text)
    assert money is not None
    assert to_grosze(money.amount) == grosze
    assert money.currency == currency


def test_parse_money_default_currency_and_empty() -> None:
    assert parse_money("1 500", default_currency="PLN").currency == "PLN"
    assert parse_money("") is None
    assert parse_money("brak ceny") is None
    assert money_to_grosze("zł 10,05") == 1005


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 234", Decimal("1234")),
        ("1,234", Decimal("1234")),
        ("1.234.567", Decimal("1234567")),
        ("1234,5", Decimal("1234.5")),
        ("12,34", Decimal("12.34")),
        ("1,234.56", Decimal("1234.56")),
        ("1.234,56", Decimal("1234.56")),
    ],
)
def test_parse_decimal(raw: str, expected: Decimal) -> None:
    assert parse_decimal(raw) == expected


def test_parse_decimal_rejects_garbage() -> None:
    assert parse_decimal("1,23,4") is None
    assert parse_decimal("abc") is None


def test_to_grosze_rounds_half_up() -> None:
    assert to_grosze(Decimal("10.005")) == 1001
    assert to_grosze(Decimal("10.004")) == 1000


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-20", date(2026, 11, 20)),
        ("20.11.2026", date(2026, 11, 20)),
        ("pt., 20 lis 2026", date(2026, 11, 20)),
        ("Bezpłatne odwołanie do 23:59 2 grudnia 2026", date(2026, 12, 2)),
        ("przed 1 marca 2027", date(2027, 3, 1)),
        ("15 września 2027", date(2027, 9, 15)),
        ("3 października 2026", date(2026, 10, 3)),
        ("5 Oct 2026", date(2026, 10, 5)),
    ],
)
def test_parse_date_text(text: str, expected: date) -> None:
    assert parse_date_text(text) == expected


def test_parse_date_text_without_year() -> None:
    assert parse_date_text("2 gru") is None
    assert parse_date_text("2 gru", default_year=2026) == date(2026, 12, 2)
    assert parse_date_text("30 lutego 2026") is None


def test_helpers() -> None:
    assert normalize_space("  a  b​ ") == "a b"
    assert fold("Bezzwrotna Taryfa") == "bezzwrotna taryfa"
    assert parse_percent("Nie zawiera: 8% VAT") == Decimal("8")
    assert parse_percent("VAT 8,5 %") == Decimal("8.5")
    assert first_int("Maks. liczba osób: 4") == 4
    assert detect_currency("cena w PLN") == "PLN"
    assert detect_currency("SPLNX") is None

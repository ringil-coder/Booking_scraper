"""Parser strony oferty Booking.com — czysta funkcja na wyrenderowanym HTML.

Kontrakt (PLAN_DZIALANIA.md P2):

    parse_property_page(html: str, *, url: str | None = None) -> ParsedPage

Implementacja: `booking_monitor.parsing.booking`.
"""

from booking_monitor.parsing.booking import PARSER_VERSION, parse_property_page

__all__ = ["PARSER_VERSION", "parse_property_page"]

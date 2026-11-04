"""Tymczasowa zaślepka parsera — zastępowana implementacją w etapie E3."""

from __future__ import annotations

from booking_monitor.models import Availability, ParsedPage, TechnicalReason

PARSER_VERSION = "booking-0.0.0"


def parse_property_page(html: str, *, url: str | None = None) -> ParsedPage:
    return ParsedPage(
        availability=Availability.UNKNOWN,
        technical_reason=TechnicalReason.PARSE_ERROR,
        message="parser nie jest jeszcze zaimplementowany",
        parser_version=PARSER_VERSION,
    )

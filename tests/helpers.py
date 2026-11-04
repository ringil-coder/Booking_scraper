"""Wspólne dane testowe: konfiguracja z realistycznymi adresami Booking (bez sieci)."""

from __future__ import annotations

from datetime import UTC, datetime

from booking_monitor.config import AppConfig, parse_competitors, parse_settings

SETTINGS_YAML = """
report_timezone: Europe/Warsaw
own_apartment: {id: moj_apartament, name: Mój apartament}
profile:
  adults: 2
  children_ages: []
  units: 1
  currency: PLN
  locale: pl-PL
collection:
  horizon_months: 3
  stay_nights: [2, 3, 4]
schedule:
  enabled: true
  interval_hours: 72
  anchor_at_utc: '2026-11-04T05:17:00Z'
  gate_cron_utc: '17 5 * * *'
"""

COMPETITORS_CSV = """competitor_key;name;booking_url;unit_filter;enabled
alfa;Apartament Alfa;https://www.booking.com/hotel/pl/apartament-alfa.pl.html?aid=1#x;;true
beta;Beta Studio;https://www.booking.com/hotel/pl/beta-studio.html;Studio;true
gamma;Gamma Lofts;https://www.booking.com/hotel/pl/gamma-lofts.en-gb.html;;true
"""


def make_config(settings_yaml: str = SETTINGS_YAML, competitors_csv: str = COMPETITORS_CSV):
    return AppConfig(
        settings=parse_settings(settings_yaml), competitors=parse_competitors(competitors_csv)
    )


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


__all__ = ["COMPETITORS_CSV", "SETTINGS_YAML", "AppConfig", "make_config", "utc"]

"""Adres strony oferty z parametrami zapytania (A§10 pkt 1).

Sam adres nie jest dowodem parametrów — po wyrenderowaniu strony kontekst
potwierdza parser (`PageContext`), a porównuje potok obserwacji.
"""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit

from booking_monitor.config import Profile
from booking_monitor.models import StayQuery


def build_property_url(canonical_url: str, query: StayQuery, profile: Profile) -> str:
    """Kanoniczny adres oferty + parametry w stałej kolejności."""
    parts = urlsplit(canonical_url)
    if parts.query or parts.fragment:
        raise ValueError(f"oczekiwano adresu kanonicznego bez parametrów: {canonical_url}")
    params: list[tuple[str, str]] = [
        ("checkin", query.checkin.isoformat()),
        ("checkout", query.checkout.isoformat()),
        ("group_adults", str(profile.adults)),
        ("group_children", str(len(profile.children_ages))),
    ]
    params += [("age", str(age)) for age in profile.children_ages]
    params += [
        ("no_rooms", str(profile.units)),
        ("selected_currency", profile.currency),
        ("lang", profile.language),
    ]
    return f"{canonical_url}?{urlencode(params)}"

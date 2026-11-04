from datetime import date

import pytest

from booking_monitor.config import Profile
from booking_monitor.models import StayQuery
from booking_monitor.providers.urls import build_property_url

CANONICAL = "https://www.booking.com/hotel/pl/testowy-apartament.html"


def query(nights: int = 3) -> StayQuery:
    return StayQuery.build("konkurent_a", date(2026, 11, 20), nights, "p_test")


def test_build_property_url_default_profile() -> None:
    url = build_property_url(CANONICAL, query(), Profile(adults=2))
    assert url == (
        CANONICAL + "?checkin=2026-11-20&checkout=2026-11-23&group_adults=2&group_children=0"
        "&no_rooms=1&selected_currency=PLN&lang=pl"
    )


def test_build_property_url_with_children_in_order() -> None:
    profile = Profile(adults=2, children_ages=(4, 10), units=1, currency="EUR", locale="en-GB")
    url = build_property_url(CANONICAL, query(nights=2), profile)
    assert url == (
        CANONICAL + "?checkin=2026-11-20&checkout=2026-11-22&group_adults=2&group_children=2"
        "&age=4&age=10&no_rooms=1&selected_currency=EUR&lang=en"
    )


def test_build_property_url_is_deterministic() -> None:
    profile = Profile(adults=2)
    assert build_property_url(CANONICAL, query(), profile) == build_property_url(
        CANONICAL, query(), profile
    )


def test_build_property_url_rejects_non_canonical() -> None:
    with pytest.raises(ValueError):
        build_property_url(CANONICAL + "?aid=1", query(), Profile(adults=2))

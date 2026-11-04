"""Provider przeglądarkowy: testy bez przeglądarki oraz (oznaczone `browser`) z Chromium.

Testy `browser` nigdy nie łączą się z siecią — wszystkie żądania są przechwytywane
przez `context.route` i obsługiwane syntetycznymi próbkami. Uruchomienie lokalne:

    BOOKING_MONITOR_CHROMIUM=/opt/pw-browsers/chromium uv run pytest -m browser
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from booking_monitor.config import ExecutionSettings, Profile
from booking_monitor.models import Availability, StayQuery, TechnicalReason
from booking_monitor.parsing import parse_property_page
from booking_monitor.providers.base import FetchError
from booking_monitor.providers.booking_browser import (
    BookingBrowserProvider,
    RateLimiter,
    parse_retry_after,
)
from booking_monitor.storage.state import CompetitorSnapshot

FIXTURES = Path(__file__).parent / "fixtures" / "booking"
CANONICAL = "https://www.booking.com/hotel/pl/testowy-apartament.html"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_rate_limiter_enforces_minimum_interval() -> None:
    clock = FakeClock()
    limiter = RateLimiter(10, clock=clock, sleep=clock.sleep)
    assert limiter.wait() == 0  # pierwsza nawigacja bez czekania
    clock.now += 3
    assert limiter.wait() == pytest.approx(7)
    clock.now += 12
    assert limiter.wait() == 0
    assert clock.sleeps == [pytest.approx(7)]


def test_rate_limiter_zero_interval_never_sleeps() -> None:
    clock = FakeClock()
    limiter = RateLimiter(0, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.wait()
    assert clock.sleeps == []


def test_parse_retry_after_seconds_and_http_date() -> None:
    now = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(" 5 ") == 5.0
    assert parse_retry_after("Wed, 04 Nov 2026 12:02:30 GMT", now=now) == 150.0
    assert parse_retry_after("Wed, 04 Nov 2026 11:00:00 GMT", now=now) == 0.0
    assert parse_retry_after("jutro") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None


def test_provider_is_lazy_and_closes_without_browser() -> None:
    provider = BookingBrowserProvider(ExecutionSettings())
    provider.close()  # nic nie uruchomiono — zamknięcie musi być bezpieczne
    assert provider.save_screenshot(Path("nie-istnieje.png")) is False


# --------------------------------------------------------------------------- z przeglądarką


def _chromium_available() -> bool:
    exe = os.environ.get("BOOKING_MONITOR_CHROMIUM")
    if exe:
        return Path(exe).exists()
    return (
        shutil.which("chromium") is not None
        or Path.home().joinpath(".cache/ms-playwright").exists()
    )


def _route(provider: BookingBrowserProvider, *, status: int, body: str, headers=None) -> list[str]:
    seen: list[str] = []

    def handler(route) -> None:
        url = route.request.url
        seen.append(url)
        if url.startswith(CANONICAL):
            route.fulfill(
                status=status,
                headers={"content-type": "text/html; charset=utf-8", **(headers or {})},
                body=body,
            )
        else:
            route.abort()  # żadnych żądań poza syntetyczną stroną

    provider.open("pl-PL").context.route("**/*", handler)
    return seen


@pytest.fixture
def browser_provider(tmp_path: Path):
    if not _chromium_available():
        pytest.skip("brak Chromium (ustaw BOOKING_MONITOR_CHROMIUM)")
    execution = ExecutionSettings(navigation_timeout_seconds=5, min_navigation_interval_seconds=0)
    with BookingBrowserProvider(execution, diagnostics_dir=tmp_path / "diag") as provider:
        yield provider


def _args():
    competitor = CompetitorSnapshot(
        competitor_id="konkurent_a",
        name="Konkurent A",
        booking_url=CANONICAL,
        canonical_url=CANONICAL,
    )
    query = StayQuery.build("konkurent_a", date(2026, 11, 20), 3, "p_test")
    return competitor, query, Profile(adults=2)


@pytest.mark.browser
def test_fetch_available_fixture(browser_provider: BookingBrowserProvider) -> None:
    html = (FIXTURES / "available_multi.html").read_text(encoding="utf-8")
    seen = _route(browser_provider, status=200, body=html)
    snapshot = browser_provider.fetch(*_args())
    assert snapshot.http_status == 200
    assert snapshot.requested_url.startswith(CANONICAL + "?checkin=2026-11-20")
    assert seen and all(url.startswith(CANONICAL) for url in seen)
    parsed = parse_property_page(snapshot.html)
    assert parsed.availability is Availability.AVAILABLE
    assert len(parsed.offers) == 3
    assert {"rate_limit_wait", "navigation", "wait_ready", "total"} <= set(snapshot.timings)


@pytest.mark.browser
def test_fetch_http_429_reports_retry_after(browser_provider: BookingBrowserProvider) -> None:
    _route(browser_provider, status=429, body="Too many requests", headers={"Retry-After": "90"})
    with pytest.raises(FetchError) as info:
        browser_provider.fetch(*_args())
    assert info.value.reason is TechnicalReason.NETWORK_ERROR
    assert info.value.retry_after_seconds == 90.0


@pytest.mark.browser
def test_fetch_challenge_page_is_blocked_with_diagnostics(
    browser_provider: BookingBrowserProvider,
) -> None:
    html = (FIXTURES / "captcha_challenge.html").read_text(encoding="utf-8")
    _route(browser_provider, status=200, body=html)
    with pytest.raises(FetchError) as info:
        browser_provider.fetch(*_args())
    assert info.value.reason is TechnicalReason.BLOCKED
    suffixes = {Path(p).suffix for p in info.value.diagnostics}
    assert suffixes == {".html", ".png"}


@pytest.mark.browser
def test_fetch_http_403_is_blocked(browser_provider: BookingBrowserProvider) -> None:
    _route(browser_provider, status=403, body="Forbidden")
    with pytest.raises(FetchError) as info:
        browser_provider.fetch(*_args())
    assert info.value.reason is TechnicalReason.BLOCKED

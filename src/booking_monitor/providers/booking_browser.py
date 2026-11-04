"""Provider Booking.com: Playwright + Chromium bez okna (A§5, A§10).

Zasady (A§10, PLAN_DZIALANIA.md R1):
- jedna przeglądarka, jeden kontekst, JEDNA karta używana ponownie;
- odstęp między nawigacjami co najmniej `min_navigation_interval_seconds`;
- domyślne ustawienia Playwright + pl-PL, Europe/Warsaw i rozmiar okna desktop;
  żadnego obchodzenia zabezpieczeń (bez „stealth”, podmiany User-Agent, proxy, CAPTCHA);
- CAPTCHA / wyzwanie / HTTP 403 → `FetchError(BLOCKED)` i koniec — decyzję o
  wznowieniu podejmuje człowiek.

Provider nie interpretuje cen. Parser służy tu tylko do rozpoznania blokady i do
sprawdzenia, czy strona zawiera cokolwiek do zinterpretowania.
"""

from __future__ import annotations

import contextlib
import email.utils
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booking_monitor.config import ExecutionSettings, Profile
from booking_monitor.models import StayQuery, TechnicalReason
from booking_monitor.parsing import parse_property_page
from booking_monitor.parsing import selectors as sel
from booking_monitor.providers.base import FetchError, PageSnapshot
from booking_monitor.providers.urls import build_property_url
from booking_monitor.redaction import scrub_html, scrub_url
from booking_monitor.storage.state import CompetitorSnapshot

if TYPE_CHECKING:
    from playwright.sync_api import Page

CHROMIUM_ENV = "BOOKING_MONITOR_CHROMIUM"
TIMEZONE_ID = "Europe/Warsaw"
VIEWPORT = {"width": 1366, "height": 900}
COOKIE_BANNER_WAIT_MS = 2_000
READY_SELECTOR = ", ".join(
    (*sel.AVAILABILITY_TABLE, *sel.UNAVAILABLE_SELECTORS, *sel.BLOCK_SELECTORS)
)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Nagłówek Retry-After: liczba sekund albo data HTTP → sekundy oczekiwania."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(int(value))
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max(0.0, (when - reference).total_seconds())


class RateLimiter:
    """Minimalny odstęp między kolejnymi nawigacjami (zegar i sen wstrzykiwane w testach)."""

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._min_interval = max(0.0, float(min_interval_seconds))
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> float:
        """Czeka, jeśli trzeba, i zapisuje moment nawigacji. Zwraca czas oczekiwania."""
        waited = 0.0
        if self._last is not None:
            remaining = self._min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
                waited = remaining
        self._last = self._clock()
        return waited


def _short(exc: BaseException, limit: int = 200) -> str:
    first_line = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
    return first_line[:limit]


class BookingBrowserProvider:
    """Implementacja `providers.base.PageProvider` dla stron ofert Booking.com."""

    def __init__(
        self,
        execution: ExecutionSettings,
        *,
        diagnostics_dir: Path | None = None,
        headless: bool = True,
        executable_path: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._timeout_ms = int(execution.navigation_timeout_seconds * 1000)
        self._limiter = RateLimiter(
            execution.min_navigation_interval_seconds, clock=clock, sleep=sleep
        )
        self._clock = clock
        self._now = now
        self._diagnostics_dir = diagnostics_dir
        self._headless = headless
        self._executable_path = executable_path or os.environ.get(CHROMIUM_ENV) or None
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Page | None = None
        self._cookies_checked = False

    # ------------------------------------------------------------------ cykl życia

    def __enter__(self) -> BookingBrowserProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def open(self, locale: str = "pl-PL") -> Page:
        """Leniwie uruchamia Chromium i zwraca jedyną kartę."""
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        launch: dict[str, Any] = {"headless": self._headless}
        if self._executable_path:
            launch["executable_path"] = self._executable_path
        self._browser = self._playwright.chromium.launch(**launch)
        self._context = self._browser.new_context(
            locale=locale, timezone_id=TIMEZONE_ID, viewport=VIEWPORT
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(self._timeout_ms)
        return self._page

    def close(self) -> None:
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._playwright and self._playwright.stop(),
        ):
            # Sprzątanie nie może maskować wyniku odczytu.
            with contextlib.suppress(Exception):
                closer()
        self._playwright = self._browser = self._context = self._page = None
        self._cookies_checked = False

    # ------------------------------------------------------------------ odczyt

    def fetch(
        self, competitor: CompetitorSnapshot, query: StayQuery, profile: Profile
    ) -> PageSnapshot:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        url = build_property_url(competitor.canonical_url, query, profile)
        page = self.open(profile.locale)
        tag = f"{competitor.competitor_id}_{query.checkin.isoformat()}_{query.nights}n"
        timings: dict[str, float] = {"rate_limit_wait": self._limiter.wait()}

        started = self._clock()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise self._error(
                TechnicalReason.TIMEOUT, f"przekroczono czas nawigacji: {_short(exc)}", tag
            ) from exc
        except PlaywrightError as exc:
            raise self._error(
                TechnicalReason.NETWORK_ERROR, f"błąd sieci: {_short(exc)}", tag
            ) from exc
        timings["navigation"] = self._clock() - started

        status = response.status if response is not None else None
        if status == 429:
            retry_after = parse_retry_after(response.headers.get("retry-after"), now=self._now())
            raise self._error(
                TechnicalReason.NETWORK_ERROR,
                "HTTP 429 — zbyt wiele żądań",
                tag,
                retry_after_seconds=retry_after,
            )
        if status == 403:
            raise self._error(TechnicalReason.BLOCKED, "HTTP 403 — odmowa dostępu", tag)
        if status is not None and status >= 400:
            raise self._error(TechnicalReason.NETWORK_ERROR, f"HTTP {status}", tag)

        self._dismiss_cookie_banner(page)
        ready_started = self._clock()
        ready = self._wait_ready(page, PlaywrightTimeoutError)
        timings["wait_ready"] = self._clock() - ready_started

        try:
            html = page.content()
        except PlaywrightError as exc:
            raise self._error(
                TechnicalReason.NETWORK_ERROR, f"nie udało się odczytać strony: {_short(exc)}", tag
            ) from exc

        parsed = parse_property_page(html, url=url)
        if parsed.technical_reason is TechnicalReason.BLOCKED:
            raise self._error(TechnicalReason.BLOCKED, parsed.message or "blokada", tag)
        if not ready and parsed.technical_reason is not None:
            raise self._error(
                TechnicalReason.TIMEOUT,
                "nie pojawiła się sekcja dostępności ani komunikat o niedostępności",
                tag,
            )
        timings["total"] = self._clock() - started
        return PageSnapshot(
            requested_url=url,
            final_url=scrub_url(page.url),
            http_status=status,
            html=html,
            fetched_at=self._now(),
            timings=timings,
        )

    def save_screenshot(self, path: Path) -> bool:
        """Zrzut bieżącej karty (np. dla pilotażu). False, gdy się nie udało."""
        if self._page is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=str(path), full_page=True)
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------ pomocnicze

    def _wait_ready(self, page: Page, timeout_error: type[Exception]) -> bool:
        try:
            page.wait_for_selector(READY_SELECTOR, state="attached", timeout=self._timeout_ms)
        except timeout_error:
            return False
        return True

    def _dismiss_cookie_banner(self, page: Page) -> None:
        """Odrzuca opcjonalne pliki cookie albo zamyka baner; brak banera nie jest błędem."""
        if self._cookies_checked:
            return
        self._cookies_checked = True
        candidates = (*sel.COOKIE_REJECT_SELECTORS, *sel.COOKIE_CLOSE_SELECTORS)
        try:
            page.wait_for_selector(
                ", ".join(candidates), state="visible", timeout=COOKIE_BANNER_WAIT_MS
            )
        except Exception:
            return
        for selector in candidates:
            try:
                button = page.locator(selector).first
                if button.is_visible():
                    button.click(timeout=COOKIE_BANNER_WAIT_MS)
                    return
            except Exception:
                continue

    def _error(
        self,
        reason: TechnicalReason,
        message: str,
        tag: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> FetchError:
        return FetchError(
            reason,
            message,
            retry_after_seconds=retry_after_seconds,
            diagnostics=self._save_diagnostics(tag, reason),
        )

    def _save_diagnostics(self, tag: str, reason: TechnicalReason) -> tuple[str, ...]:
        """HTML i zrzut ekranu przy błędzie. Bez cookies, nagłówków i danych sesji."""
        if self._diagnostics_dir is None or self._page is None:
            return ()
        stamp = self._now().strftime("%Y%m%dT%H%M%SZ")
        base = self._diagnostics_dir / f"{stamp}_{tag}_{reason.value}"
        saved: list[str] = []
        try:
            self._diagnostics_dir.mkdir(parents=True, exist_ok=True)
            html_path = base.with_suffix(".html")
            # Artefakty publicznego repozytorium: bez identyfikatora sesji i tokenów.
            html_path.write_text(scrub_html(self._page.content()), encoding="utf-8")
            saved.append(str(html_path))
        except Exception:
            pass
        png_path = base.with_suffix(".png")
        if self.save_screenshot(png_path):
            saved.append(str(png_path))
        return tuple(saved)

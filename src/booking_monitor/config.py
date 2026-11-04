"""Wczytywanie i walidacja konfiguracji (ARCHITEKTURA.md §2, §12).

Jeden własny apartament, jeden profil i jedna lista konkurentów.
Placeholdery i sprzeczne wpisy są odrzucane, zanim cokolwiek otworzy przeglądarkę.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from booking_monitor.models import DEFAULT_SOURCE, FrozenModel

PLACEHOLDER_MARKER = "UZUPELNIJ"
COMPETITOR_COLUMNS = ("competitor_key", "name", "booking_url", "unit_filter", "enabled")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# /hotel/<kraj>/<slug>[.<język>].html
_HOTEL_PATH_RE = re.compile(
    r"^/hotel/(?P<cc>[a-z]{2})/(?P<slug>[a-z0-9][a-z0-9-]*?)(?:\.[a-z]{2}(?:-[a-z]{2,4})?)?\.html$"
)


class ConfigError(ValueError):
    """Błąd konfiguracji; `problems` zawiera wszystkie wykryte usterki."""

    def __init__(self, problems: Iterable[str]):
        self.problems = list(problems)
        super().__init__("Błędy konfiguracji:\n- " + "\n- ".join(self.problems))


def _stable_hash(payload: object, length: int = 16) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


# --------------------------------------------------------------------------- ustawienia


class OwnApartment(FrozenModel):
    id: str
    name: str
    booking_url: str | None = None


class Profile(FrozenModel):
    adults: int = Field(ge=1, le=30)
    children_ages: tuple[int, ...] = ()
    units: int = Field(default=1, ge=1, le=10)
    currency: str = "PLN"
    locale: str = "pl-PL"
    device: Literal["desktop"] = "desktop"
    logged_in: Literal[False] = False
    accommodation_type: Literal["entire_apartment"] = "entire_apartment"

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("waluta musi być kodem ISO 4217, np. PLN")
        return value

    @field_validator("children_ages")
    @classmethod
    def _children(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(age < 0 or age > 17 for age in value):
            raise ValueError("wiek dziecka musi być w zakresie 0–17")
        return value

    @property
    def guests(self) -> int:
        return self.adults + len(self.children_ages)

    @property
    def language(self) -> str:
        """Kod języka Booking, np. 'pl' dla 'pl-PL'."""
        return self.locale.split("-")[0].lower()

    def profile_hash(self) -> str:
        return "p_" + _stable_hash(self.model_dump(mode="json"))


class CollectionSettings(FrozenModel):
    provider: Literal["booking_browser"] = "booking_browser"
    horizon_months: int = Field(default=3, ge=1, le=12)
    stay_nights: tuple[int, ...] = (2, 3, 4)
    refresh_mode: Literal["full_cycle"] = "full_cycle"

    @field_validator("stay_nights")
    @classmethod
    def _nights(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("lista długości pobytu nie może być pusta")
        if any(n < 1 or n > 30 for n in value):
            raise ValueError("długość pobytu musi być w zakresie 1–30 nocy")
        if len(set(value)) != len(value):
            raise ValueError("długości pobytu nie mogą się powtarzać")
        return tuple(sorted(value))

    @property
    def source(self) -> str:
        return DEFAULT_SOURCE


class ScheduleSettings(FrozenModel):
    enabled: bool = False
    interval_hours: int = Field(default=72, ge=1)
    anchor_at_utc: datetime
    gate_cron_utc: str = "17 5 * * *"

    @field_validator("anchor_at_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("anchor_at_utc musi zawierać strefę (np. sufiks Z)")
        return value.astimezone(UTC)


class ExecutionSettings(FrozenModel):
    parallel_jobs: Literal[1] = 1
    browser_pages: Literal[1] = 1
    navigation_timeout_seconds: int = Field(default=45, ge=5)
    min_navigation_interval_seconds: float = Field(default=10, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_delay_seconds: int = Field(default=60, ge=0)
    max_inline_retry_wait_seconds: int = Field(default=300, ge=0)
    collection_budget_minutes_per_job: int = Field(default=240, ge=1)
    job_timeout_minutes: int = Field(default=300, ge=1)
    checkpoint_interval_minutes: int = Field(default=15, ge=1)
    checkpoint_max_observations: int = Field(default=100, ge=1)
    max_consecutive_parse_errors: int = Field(default=5, ge=1)
    estimated_observation_seconds_p95: float = Field(default=20, gt=0)
    planning_margin: float = Field(default=0.25, ge=0)
    max_windows_per_run: int = Field(default=12, ge=1, le=256)

    @model_validator(mode="after")
    def _budget_below_timeout(self) -> ExecutionSettings:
        if self.collection_budget_minutes_per_job >= self.job_timeout_minutes:
            raise ValueError("budżet zbierania musi być mniejszy niż timeout joba")
        return self


class StorageSettings(FrozenModel):
    state_branch: str = "monitor-data"
    working_database: str = ".runtime/monitor.sqlite3"
    history_retention_days: int = Field(default=365, ge=1)
    report_artifact_retention_days: int = Field(default=30, ge=1)
    diagnostics_artifact_retention_days: int = Field(default=7, ge=1)

    @field_validator("state_branch")
    @classmethod
    def _not_code_branch(cls, value: str) -> str:
        # Workflow zbierający zapisuje wyłącznie gałąź danych, nigdy gałęzi kodu (A§13).
        if value in {"main", "master"} or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", value):
            raise ValueError("state_branch musi być osobną gałęzią danych, np. monitor-data")
        return value


class Settings(FrozenModel):
    report_timezone: str = "Europe/Warsaw"
    own_apartment: OwnApartment
    profile: Profile
    collection: CollectionSettings = CollectionSettings()
    schedule: ScheduleSettings
    execution: ExecutionSettings = ExecutionSettings()
    storage: StorageSettings = StorageSettings()

    @field_validator("report_timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"nieznana strefa czasowa: {value}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.report_timezone)


# --------------------------------------------------------------------------- konkurenci


class Competitor(FrozenModel):
    competitor_id: str
    name: str
    booking_url: str
    canonical_url: str
    unit_filter: str | None = None
    enabled: bool = True


def canonicalize_booking_url(url: str) -> str:
    """Kanoniczny adres oferty: https://www.booking.com/hotel/<kraj>/<slug>.html.

    Usuwa parametry, fragment i wariant językowy (`.pl.html`, `.en-gb.html`).
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"adres musi zaczynać się od https://: {url}")
    if host != "booking.com" and not host.endswith(".booking.com"):
        raise ValueError(f"adres spoza booking.com: {url}")
    match = _HOTEL_PATH_RE.match(parts.path.lower())
    if not match:
        raise ValueError(f"to nie jest adres konkretnej oferty (/hotel/<kraj>/<nazwa>.html): {url}")
    return f"https://www.booking.com/hotel/{match['cc']}/{match['slug']}.html"


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "tak", "yes"}:
        return True
    if normalized in {"false", "0", "nie", "no"}:
        return False
    raise ValueError(f"nieprawidłowa wartość enabled: {value!r}")


def parse_competitors(text: str) -> tuple[Competitor, ...]:
    """Parsuje `competitors.csv` (separator `;`); zwraca aktywnych, unikalnych konkurentów."""
    problems: list[str] = []
    reader = csv.DictReader(text.lstrip("\ufeff").splitlines(), delimiter=";")
    if tuple(reader.fieldnames or ()) != COMPETITOR_COLUMNS:
        raise ConfigError([f"nagłówek competitors.csv musi być: {';'.join(COMPETITOR_COLUMNS)}"])

    seen_keys: dict[str, str] = {}
    by_url: dict[str, Competitor] = {}
    result: list[Competitor] = []
    for line_no, row in enumerate(reader, start=2):
        key = (row["competitor_key"] or "").strip()
        name = (row["name"] or "").strip()
        url = (row["booking_url"] or "").strip()
        unit_filter = (row["unit_filter"] or "").strip() or None
        where = f"competitors.csv:{line_no}"
        try:
            enabled = _parse_bool(row["enabled"] or "")
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
            continue
        if not _KEY_RE.match(key):
            problems.append(f"{where}: competitor_key {key!r} — dozwolone a-z, 0-9, _ i -")
            continue
        if key in seen_keys:
            problems.append(f"{where}: powtórzony competitor_key {key!r}")
            continue
        seen_keys[key] = url
        if not enabled:
            continue
        if any(
            PLACEHOLDER_MARKER in field for field in (key.upper(), name, url, unit_filter or "")
        ):
            problems.append(
                f"{where}: placeholder {PLACEHOLDER_MARKER} — uzupełnij dane konkurenta"
            )
            continue
        if not name:
            problems.append(f"{where}: brak nazwy")
            continue
        try:
            canonical = canonicalize_booking_url(url)
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
            continue
        competitor = Competitor(
            competitor_id=key,
            name=name,
            booking_url=url,
            canonical_url=canonical,
            unit_filter=unit_filter,
            enabled=True,
        )
        previous = by_url.get(canonical)
        if previous is not None:
            if previous.name != name or previous.unit_filter != unit_filter:
                problems.append(
                    f"{where}: adres {canonical} powtórzony z inną nazwą lub filtrem "
                    f"(wcześniej {previous.competitor_id!r})"
                )
            # Zgodny duplikat: ta sama oferta nie tworzy dodatkowych pomiarów (A§2).
            continue
        by_url[canonical] = competitor
        result.append(competitor)

    if problems:
        raise ConfigError(problems)
    if not result:
        raise ConfigError(["lista konkurentów nie zawiera żadnej aktywnej oferty"])
    return tuple(result)


# --------------------------------------------------------------------------- całość


class AppConfig(FrozenModel):
    settings: Settings
    competitors: tuple[Competitor, ...]

    @property
    def profile_hash(self) -> str:
        return self.settings.profile.profile_hash()

    def config_hash(self) -> str:
        """Hash konfiguracji wpływającej na plan cyklu (zapisywany w manifeście)."""
        payload = {
            "settings": self.settings.model_dump(mode="json"),
            "competitors": [c.model_dump(mode="json") for c in self.competitors],
        }
        return "cfg_" + _stable_hash(payload)

    def competitor(self, competitor_id: str) -> Competitor:
        for competitor in self.competitors:
            if competitor.competitor_id == competitor_id:
                return competitor
        raise KeyError(competitor_id)


def parse_settings(text: str) -> Settings:
    data = yaml.safe_load(text) or {}
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"settings.yaml: {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        ) from exc


def load_settings(config_dir: Path) -> Settings:
    return parse_settings((config_dir / "settings.yaml").read_text(encoding="utf-8"))


def load_config(config_dir: Path) -> AppConfig:
    settings = load_settings(config_dir)
    competitors = parse_competitors((config_dir / "competitors.csv").read_text(encoding="utf-8"))
    return AppConfig(settings=settings, competitors=competitors)

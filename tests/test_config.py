from pathlib import Path

import pytest

from booking_monitor.config import (
    ConfigError,
    canonicalize_booking_url,
    load_settings,
    parse_competitors,
    parse_settings,
)
from tests.helpers import SETTINGS_YAML, make_config

HEADER = "competitor_key;name;booking_url;unit_filter;enabled\n"
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.booking.com/hotel/pl/apartament-alfa.html",
        "https://www.booking.com/hotel/pl/apartament-alfa.pl.html",
        "http://booking.com/hotel/pl/apartament-alfa.en-gb.html?checkin=2026-01-01#map",
        "https://m.booking.com/hotel/pl/Apartament-Alfa.pl.html",
    ],
)
def test_canonical_url_variants(url):
    assert canonicalize_booking_url(url) == "https://www.booking.com/hotel/pl/apartament-alfa.html"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.booking.com/searchresults.pl.html?ss=Gdansk",
        "https://www.example.com/hotel/pl/x.html",
        "ftp://www.booking.com/hotel/pl/x.html",
        "UZUPELNIJ_ADRES",
    ],
)
def test_canonical_url_rejects(url):
    with pytest.raises(ValueError):
        canonicalize_booking_url(url)


def test_repository_settings_are_valid():
    settings = load_settings(ROOT / "config")
    assert settings.collection.stay_nights == (2, 3, 4)
    assert settings.schedule.enabled is False


def test_repository_competitors_are_valid():
    text = (ROOT / "config" / "competitors.csv").read_text(encoding="utf-8")
    competitors = parse_competitors(text)
    assert competitors
    assert "UZUPELNIJ" not in text
    canonical = [c.canonical_url for c in competitors]
    assert len(set(canonical)) == len(canonical)
    for url in canonical:
        assert url.startswith("https://www.booking.com/hotel/")
        assert canonicalize_booking_url(url) == url


def test_placeholder_competitor_is_rejected():
    text = HEADER + "konkurent_a;Konkurent A;UZUPELNIJ_ADRES_BOOKING_A;;true\n"
    with pytest.raises(ConfigError) as excinfo:
        parse_competitors(text)
    assert all("UZUPELNIJ" in p for p in excinfo.value.problems)


def test_duplicate_url_is_deduplicated():
    text = HEADER + (
        "a;Alfa;https://www.booking.com/hotel/pl/alfa.html;;true\n"
        "a2;Alfa;https://www.booking.com/hotel/pl/alfa.pl.html?aid=5;;true\n"
    )
    competitors = parse_competitors(text)
    assert [c.competitor_id for c in competitors] == ["a"]


def test_conflicting_duplicate_is_error():
    text = HEADER + (
        "a;Alfa;https://www.booking.com/hotel/pl/alfa.html;;true\n"
        "b;Inna nazwa;https://www.booking.com/hotel/pl/alfa.html;;true\n"
    )
    with pytest.raises(ConfigError, match="inną nazwą lub filtrem"):
        parse_competitors(text)


def test_disabled_rows_and_bom():
    text = (
        "﻿"
        + HEADER
        + ("a;Alfa;https://www.booking.com/hotel/pl/alfa.html;;true\nb;Beta;UZUPELNIJ;;false\n")
    )
    assert [c.competitor_id for c in parse_competitors(text)] == ["a"]


def test_bad_key_and_duplicate_key_reported_together():
    text = HEADER + (
        "Zły Klucz;X;https://www.booking.com/hotel/pl/x.html;;true\n"
        "a;A;https://www.booking.com/hotel/pl/a.html;;true\n"
        "a;B;https://www.booking.com/hotel/pl/b.html;;true\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        parse_competitors(text)
    assert len(excinfo.value.problems) == 2


def test_wrong_header():
    with pytest.raises(ConfigError, match="nagłówek"):
        parse_competitors("key;name\n")


def test_settings_validation_errors():
    with pytest.raises(ConfigError, match="anchor_at_utc"):
        parse_settings(SETTINGS_YAML.replace("'2026-11-04T05:17:00Z'", "'2026-11-04T05:17:00'"))
    with pytest.raises(ConfigError, match="stay_nights"):
        parse_settings(SETTINGS_YAML.replace("[2, 3, 4]", "[2, 2]"))


def test_hashes_are_stable_and_sensitive():
    first, second = make_config(), make_config()
    assert first.config_hash() == second.config_hash()
    assert first.profile_hash == second.profile_hash
    changed = make_config(SETTINGS_YAML.replace("adults: 2", "adults: 3"))
    assert changed.profile_hash != first.profile_hash
    assert changed.config_hash() != first.config_hash()

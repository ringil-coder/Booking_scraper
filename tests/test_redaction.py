"""Usuwanie danych sesji przed zapisem do publicznych artefaktów (redaction.py)."""

from __future__ import annotations

from pathlib import Path

from booking_monitor.parsing import parse_property_page
from booking_monitor.redaction import REDACTED, scrub_html, scrub_url

SID = "7c078467debc56d2543c6d8318c12ff8"
CSRF = "ovfrAbCdEfGh1234567890xyz"
LABEL = "gen000bo-10CAEoggI46AdIM1gEaLYBiAEBmAEz"

# Postaci zaobserwowane na prawdziwej stronie oferty w pilotażu E1 (wartości zmyślone).
PAGE = f"""<html><head><title>Alfa, Warszawa</title>
<script>
var booking = {{ env: {{ b_sid: '{SID}', b_csrf_token: '{CSRF}', b_hotel_id: '697316' }} }};
var sid = '{SID}'; token = '{CSRF}';
</script>
<script type="application/json">{{"b_sid":"{SID}","aid":"304142"}}</script>
</head><body>
<h2 id="hp_hotel_name">Alfa</h2>
<a href="/hotel/pl/alfa.pl.html?aid=304142&amp;sid={SID}&amp;checkin=2026-11-20">link</a>
<a href="/searchresults.pl.html;label={LABEL};sid={SID}">wyniki</a>
<form><input type="hidden" name="sid" value="{SID}">
<input type="hidden" value="{LABEL}" name="label">
<input type="hidden" name="hotel_id" value="697316">
<input type="hidden" name="checkin" value="2026-11-20"></form>
</body></html>"""


def test_scrub_html_removes_every_session_value():
    out = scrub_html(PAGE)
    for secret in (SID, CSRF, LABEL):
        assert secret not in out
    assert REDACTED in out


def test_scrub_html_keeps_page_content_for_parser():
    out = scrub_html(PAGE)
    assert 'name="hotel_id" value="697316"' in out
    assert 'name="checkin" value="2026-11-20"' in out
    assert "checkin=2026-11-20" in out and "aid=304142" in out
    before, after = parse_property_page(PAGE), parse_property_page(out)
    assert after.property_name == before.property_name == "Alfa"
    assert after.booking_property_id == before.booking_property_id
    assert after.context == before.context


def test_scrub_html_leaves_fixture_without_session_data_unchanged():
    fixture = Path(__file__).parent / "fixtures" / "booking" / "available_multi.html"
    html = fixture.read_text(encoding="utf-8")
    assert scrub_html(html) == html


def test_scrub_url_keeps_query_parameters_of_the_stay():
    url = (
        "https://www.booking.com/hotel/pl/alfa.pl.html?checkin=2026-11-20&checkout=2026-11-22"
        f"&group_adults=2&sid={SID}&label={LABEL}&selected_currency=PLN"
    )
    out = scrub_url(url)
    assert SID not in out and LABEL not in out
    assert "checkin=2026-11-20&checkout=2026-11-22&group_adults=2" in out
    assert out.endswith("&selected_currency=PLN")
    assert scrub_url("https://www.booking.com/hotel/pl/alfa.pl.html") == (
        "https://www.booking.com/hotel/pl/alfa.pl.html"
    )

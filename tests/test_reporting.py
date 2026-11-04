from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from booking_monitor.analytics import build_cycle_report
from booking_monitor.reporting.bundle import build_report_bundle, latest_report_entry
from booking_monitor.reporting.csv_export import COLUMNS, protect_cell, write_prices_csv
from booking_monitor.reporting.html import render_html
from booking_monitor.reporting.summary import render_summary_markdown
from booking_monitor.storage.events import RunContext
from tests.report_db import CURRENT_CYCLE, PREVIOUS_CYCLE, build_scenario, utc

TZ = ZoneInfo("Europe/Warsaw")
NOW = utc(2026, 11, 5, 6)


def _report(status: str = "partial", *, competitor_name: str | None = None):
    db = build_scenario(current_status=status)
    if competitor_name is not None:
        db.conn.execute(
            "UPDATE cycle_competitors SET name = ? WHERE competitor_id = 'k1'", (competitor_name,)
        )
    return db, build_cycle_report(db.conn, CURRENT_CYCLE, now=NOW, tz=TZ)


def _embedded_data(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S
    )
    assert match, "brak osadzonych danych"
    return json.loads(match.group(1))


# --------------------------------------------------------------------------- HTML


def test_html_is_self_contained():
    _, report = _report()
    html = render_html(report)
    assert "<link" not in html.lower()
    assert not re.search(r"<script[^>]*\bsrc\s*=", html, re.I)
    assert not re.search(r"\b(?:src|href|action)\s*=\s*[\"']?\s*(?:https?:)?//", html, re.I)
    assert "@import" not in html
    assert not re.search(r"url\(\s*[\"']?(?:https?:)?//", html, re.I)
    assert not re.search(r"\bfetch\(|XMLHttpRequest|WebSocket|EventSource", html)


def test_html_embeds_report_data_and_partial_banner():
    _, report = _report()
    html = render_html(report)
    data = _embedded_data(html)
    assert data["meta"]["cycleId"] == CURRENT_CYCLE
    assert data["cycles"] == [PREVIOUS_CYCLE, CURRENT_CYCLE]
    assert len(data["results"]) == 18
    assert set(data["cells"]) == {"lowest", "non_refundable", "free_cancellation"}
    assert 'id="partial-banner"' in html
    assert "Raport częściowy" in html
    assert "Mój apartament" in html
    # Brak przełącznika apartamentów (A§15 pkt 1).
    assert "apartament" not in "".join(re.findall(r'<select id="([^"]+)"', html))
    # Stara cena k6 jest osadzona jako nieaktualna, z cyklem źródłowym.
    stale = [r["st"] for r in data["results"] if r["st"]]
    assert stale and stale[0][1] == 39000 and stale[0][6] == PREVIOUS_CYCLE


def test_html_escapes_untrusted_text():
    payload = "</script><script>alert(1)</script>"
    _, report = _report(competitor_name=payload)
    html = render_html(report)
    assert html.count("</script>") == 2
    assert "<script>alert(1)" not in html
    data = _embedded_data(html)
    assert payload in [c[1] for c in data["competitors"]]


def test_completed_report_has_no_partial_banner():
    _, report = _report("completed")
    assert 'id="partial-banner"' not in render_html(report)


def test_blocked_report_shows_critical_banner():
    db = build_scenario(current_status="blocked")
    db.conn.execute(
        "UPDATE cycles SET blocked_reason = 'blocked', blocked_message = 'CAPTCHA' WHERE id = ?",
        (CURRENT_CYCLE,),
    )
    report = build_cycle_report(db.conn, CURRENT_CYCLE, now=NOW, tz=TZ)
    html = render_html(report)
    assert "Cykl zablokowany" in html
    assert "banner-critical" in html


# --------------------------------------------------------------------------- CSV


def test_csv_format_bom_separator_and_decimal_comma(tmp_path: Path):
    _, report = _report(competitor_name="=HYPERLINK(1)")
    path = write_prices_csv(report, tmp_path / "prices.csv")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    assert tuple(rows[0].keys()) == COLUMNS
    k1 = [r for r in rows if r["competitor_id"] == "k1" and r["checkin"] == "2026-11-04"]
    assert {r["total_price"] for r in k1} == {"400,00", "460,00"}
    assert [r["is_lowest"] for r in k1 if r["total_price"] == "400,00"] == ["true"]
    assert all(r["competitor_name"] == "'=HYPERLINK(1)" for r in k1)
    assert k1[0]["observed_at"] == "2026-11-04T06:00:00Z"


def test_csv_marks_stale_and_partial_rows(tmp_path: Path):
    _, report = _report()
    path = write_prices_csv(report, tmp_path / "prices.csv")
    rows = list(csv.DictReader(io.StringIO(path.read_text("utf-8-sig")), delimiter=";"))
    stale = [r for r in rows if r["stale"] == "true"]
    assert len(stale) == 1
    assert stale[0]["competitor_id"] == "k6"
    assert stale[0]["price_cycle_id"] == PREVIOUS_CYCLE
    assert stale[0]["total_price"] == "390,00"
    assert stale[0]["comparable"] == "false"
    partial = [r for r in rows if r["price_completeness"] == "partial"]
    assert partial and partial[0]["total_price"] == "" and partial[0]["comparable"] == "false"


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
def test_protect_cell_formula_prefixes(value):
    assert protect_cell(value) == "'" + value


def test_protect_cell_leaves_plain_text():
    assert protect_cell("Apartament") == "Apartament"
    assert protect_cell(None) == ""


# --------------------------------------------------------------------------- podsumowanie


def test_summary_mentions_partial_status_and_errors():
    _, report = _report()
    text = render_summary_markdown(report, artifact_name="raport-c20261104-s0001")
    assert "Częściowy" in text
    assert "raport częściowy" in text.lower()
    assert "72,2%" in text
    assert "Blokada / CAPTCHA: 1" in text
    assert "raport-c20261104-s0001" in text
    assert "Mała próba" in text


def test_summary_blocked_and_completed():
    db = build_scenario(current_status="blocked")
    report = build_cycle_report(db.conn, CURRENT_CYCLE, now=NOW, tz=TZ)
    assert "zablokowany" in render_summary_markdown(report).lower()
    _, done = _report("completed")
    assert "✅" in render_summary_markdown(done)


# --------------------------------------------------------------------------- paczka


def test_bundle_writes_files_with_checksums(tmp_path: Path):
    db, _ = _report()
    run = RunContext(workflow_run_id="42", workflow_run_attempt=1, code_sha="abc123")
    manifest = build_report_bundle(
        db.conn, CURRENT_CYCLE, tmp_path, now=NOW, tz=TZ, run=run, artifact_name="raport"
    )
    names = {f.name for f in manifest.files}
    assert names == {"report.html", "prices.csv", "summary.md", "monitor.sqlite3"}
    for entry in manifest.files:
        assert hashlib.sha256((tmp_path / entry.name).read_bytes()).hexdigest() == entry.sha256
    saved = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert saved["cycle_id"] == CURRENT_CYCLE
    assert saved["run"]["workflow_run_id"] == "42"
    assert saved["partial_quality"] is True

    copy = sqlite3.connect(tmp_path / "monitor.sqlite3")
    try:
        assert (
            copy.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            == db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        )
    finally:
        copy.close()

    latest = latest_report_entry(manifest)
    assert latest.cycle_id == CURRENT_CYCLE
    assert "manifest.json" in latest.files
    assert latest.coverage_pct == manifest.coverage_pct


def test_bundle_without_sqlite(tmp_path: Path):
    db, _ = _report()
    manifest = build_report_bundle(
        db.conn, CURRENT_CYCLE, tmp_path, now=NOW, tz=TZ, run=RunContext(), include_sqlite=False
    )
    assert "monitor.sqlite3" not in {f.name for f in manifest.files}
    assert not (tmp_path / "monitor.sqlite3").exists()


# --------------------------------------------------------------------------- przeglądarka


@pytest.mark.browser
def test_html_runs_in_browser_without_requests(tmp_path: Path):
    """Wymaga Chromium (Playwright); pomijany w zwykłym CI."""
    sync_api = pytest.importorskip("playwright.sync_api")
    _, report = _report()
    path = tmp_path / "report.html"
    path.write_text(render_html(report), encoding="utf-8")
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if not executable and Path("/opt/pw-browsers/chromium").exists():
        executable = "/opt/pw-browsers/chromium"
    errors: list[str] = []
    requests: list[str] = []
    with sync_api.sync_playwright() as p:
        browser = (
            p.chromium.launch(executable_path=executable) if executable else (p.chromium.launch())
        )
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: requests.append(r.url))
        page.goto(path.as_uri())
        page.click("[data-tab=calendar]")
        cells = page.locator(".cal-cell").count()
        page.select_option("#f-meal", "breakfast")
        page.click("[data-tab=competitors]")
        rows = page.locator("#comp-table tbody tr").count()
        browser.close()
    assert errors == []
    assert requests == [path.as_uri()]
    assert cells == 2 * 3
    assert rows == 18


def test_plural_pl():
    from booking_monitor.reporting.html import plural_pl

    forms = ("błąd", "błędy", "błędów")
    assert [plural_pl(n, *forms) for n in (0, 1, 2, 5, 12, 22, 25)] == [
        "0 błędów",
        "1 błąd",
        "2 błędy",
        "5 błędów",
        "12 błędów",
        "22 błędy",
        "25 błędów",
    ]

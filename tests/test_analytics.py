from __future__ import annotations

import json
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from booking_monitor.analytics import (
    MODE_LOWEST,
    OfferView,
    ReportError,
    build_cycle_report,
    comparable_offer,
    format_money,
    median_grosze,
)
from tests.report_db import (
    CURRENT_CYCLE,
    D4,
    D5,
    PREVIOUS_CYCLE,
    build_scenario,
    utc,
)

TZ = ZoneInfo("Europe/Warsaw")
NOW = utc(2026, 11, 5, 6)


@pytest.fixture
def report():
    db = build_scenario()
    return build_cycle_report(db.conn, CURRENT_CYCLE, now=NOW, tz=TZ)


def test_lowest_mode_one_price_per_competitor(report):
    cell = report.cell(D4, 2, MODE_LOWEST)
    # k1: 400 zł (najtańsza z dwóch taryf), k2: 500 zł; pozostali bez porównywalnej ceny.
    assert cell.n_comparable == 2
    assert cell.n_competitors == 6
    assert cell.min_total_grosze == 40000
    assert cell.median_total_grosze == 45000
    assert cell.small_sample is True


def test_category_modes_use_selected_offer_of_category(report):
    non_refundable = report.cell(D4, 2, "non_refundable")
    assert (non_refundable.n_comparable, non_refundable.min_total_grosze) == (1, 40000)
    free = report.cell(D4, 2, "free_cancellation")
    # k2: niewybrana droższa oferta ze śniadaniem nie wnosi drugiej ceny.
    assert free.n_comparable == 2
    assert free.median_total_grosze == 48000


def test_partial_price_and_other_currency_are_excluded(report):
    k3 = report.result("k3", D4, 2)
    assert k3.availability == "available"
    assert k3.comparable is False
    assert k3.non_comparable_reason == "partial_price"
    k6 = report.result("k6", D5, 2)
    assert k6.non_comparable_reason == "currency_mismatch"
    cell = report.cell(D5, 2, MODE_LOWEST)
    assert (cell.n_comparable, cell.min_total_grosze) == (1, 55000)


def test_no_matching_offer_and_unavailable(report):
    assert report.result("k4", D4, 2).non_comparable_reason == "no_matching_offer"
    k5 = report.result("k5", D4, 2)
    assert k5.availability == "unavailable"
    assert k5.comparable is False
    # Nowa niedostępność: stara cena zostaje tylko w historii.
    assert k5.stale is None


def test_median_with_rounding_and_full_sample(report):
    cell = report.cell(D5, 3, MODE_LOWEST)
    assert cell.n_comparable == 6
    assert cell.small_sample is False
    assert cell.min_total_grosze == 60000
    assert cell.median_total_grosze == 62500
    # (20667 + 21000) / 2 = 20833,5 → 20834 (połówka grosza w górę)
    assert cell.median_avg_night_grosze == 20834
    assert cell.min_avg_night_grosze == 20000


def test_stale_price_shown_but_never_in_statistics(report):
    k6 = report.result("k6", D4, 2)
    assert k6.job_status == "failed"
    assert k6.technical_reason == "blocked"
    assert k6.stale is not None
    assert k6.stale.cycle_id == PREVIOUS_CYCLE
    assert k6.stale.total_price_grosze == 39000
    assert k6.stale.observed_at == utc(2026, 11, 1, 8)
    cell = report.cell(D4, 2, MODE_LOWEST)
    assert 39000 not in (cell.min_total_grosze, cell.median_total_grosze)
    assert cell.n_comparable == 2


def test_history_change_vs_previous_cycle(report):
    series = {(h.competitor_id, h.checkin, h.nights): h for h in report.history}
    k1 = series[("k1", D4, 2)]
    assert [p.total_price_grosze for p in k1.points] == [42000, 40000]
    assert k1.change_vs_previous_grosze == -2000
    assert k1.change_vs_previous_pct == -4.8
    k5 = series[("k5", D4, 2)]
    assert [p.availability for p in k5.points] == ["available", "unavailable"]
    assert k5.change_vs_previous_grosze is None
    # Zapytanie bez bieżącej obserwacji i z jednym punktem nie trafia do historii.
    assert ("k6", D4, 2) not in series
    assert report.history_cycles == (PREVIOUS_CYCLE, CURRENT_CYCLE)


def test_overview_coverage_and_observation_span(report):
    ov = report.overview
    assert ov.apartment_name == "Mój apartament"
    assert ov.jobs_total == 18
    assert ov.job_counts["completed"] == 13
    assert ov.job_counts["failed"] == 2
    assert ov.job_counts["expired"] == 1
    assert ov.job_counts["pending"] == 1
    assert ov.job_counts["retry_wait"] == 1
    assert ov.coverage_pct == 72.2
    assert ov.oldest_observed_at == utc(2026, 11, 4, 6, 0)
    assert ov.newest_observed_at == utc(2026, 11, 4, 19, 30)
    assert ov.comparable_observations == 9
    assert ov.non_comparable_observations == 4
    assert ov.partial_quality is True
    assert ov.parser_versions == ("booking-0.1.0",)


def test_data_state_counts(report):
    ds = report.data_state
    assert ds.executed == 13
    assert ds.missing == 2
    assert ds.expired == 1
    assert ds.failed_by_reason == {"blocked": 1, "parse_error": 1}
    assert ds.unavailable == 1
    assert ds.non_comparable_partial_price == 1
    assert ds.non_comparable_no_matching_offer == 1
    assert ds.non_comparable_other == 1
    k1 = next(c for c in ds.per_competitor if c.competitor_id == "k1")
    assert (k1.total, k1.completed, k1.expired) == (3, 2, 1)


def test_calendar_covers_whole_window(report):
    # Okno 04–05.11 × 3 długości × 3 tryby; puste terminy mają n = 0.
    assert len(report.calendar) == 2 * 3 * 3
    empty = report.cell(D4, 4, MODE_LOWEST)
    assert empty.n_comparable == 0
    assert empty.min_total_grosze is None


def test_completed_cycle_is_not_partial():
    db = build_scenario(current_status="completed")
    report = build_cycle_report(db.conn, CURRENT_CYCLE, now=NOW, tz=TZ)
    assert report.overview.partial_quality is False


def test_report_is_json_serialisable(report):
    data = json.loads(report.model_dump_json())
    assert data["overview"]["cycle_id"] == CURRENT_CYCLE


def test_missing_cycle_raises():
    db = build_scenario()
    with pytest.raises(ReportError):
        build_cycle_report(db.conn, "c20990101-s9999", now=NOW, tz=TZ)


def _offer(**kwargs) -> OfferView:
    base = {
        "offer_index": 0,
        "unit_name": "Apartament",
        "rate_category": "free_cancellation",
        "total_price_grosze": 50000,
        "avg_night_price_grosze": 25000,
        "displayed_price_grosze": 50000,
        "currency": "PLN",
        "price_completeness": "complete",
        "meal_plan": "room_only",
        "prepayment": "no_prepayment",
        "selected_for_category": True,
        "comparable": True,
    }
    base.update(kwargs)
    return OfferView(**base)


def test_equal_conditions_filter_uses_matching_offers_only():
    offers = [
        _offer(offer_index=0, total_price_grosze=40000, meal_plan="room_only"),
        _offer(
            offer_index=1,
            total_price_grosze=45000,
            meal_plan="breakfast",
            selected_for_category=False,
        ),
        _offer(offer_index=2, total_price_grosze=30000, meal_plan="unknown"),
    ]
    assert comparable_offer(offers, MODE_LOWEST).total_price_grosze == 30000
    breakfast = comparable_offer(offers, "free_cancellation", meal_plan="breakfast")
    assert breakfast.total_price_grosze == 45000
    # „unknown” nie pasuje do znanej wartości filtra.
    room_only = comparable_offer(offers, MODE_LOWEST, meal_plan="room_only")
    assert room_only.total_price_grosze == 40000
    assert comparable_offer(offers, MODE_LOWEST, prepayment="prepayment_required") is None


def test_non_comparable_offers_never_selected():
    offers = [_offer(total_price_grosze=None, comparable=False, price_completeness="partial")]
    assert comparable_offer(offers, MODE_LOWEST) is None


def test_format_money_polish():
    assert format_money(123456789) == "1 234 567,89 zł"
    assert format_money(5) == "0,05 zł"
    assert format_money(None) == "—"
    assert format_money(1000, "EUR") == "10,00 EUR"


def test_median_grosze():
    assert median_grosze([]) is None
    assert median_grosze([3, 1, 2]) == 2
    assert median_grosze([1, 2]) == 2
    assert median_grosze([100, 200]) == 150


def test_report_dates_are_dates(report):
    assert isinstance(report.results[0].checkin, date)

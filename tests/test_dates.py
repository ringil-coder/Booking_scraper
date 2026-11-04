from datetime import date
from zoneinfo import ZoneInfo

import pytest

from booking_monitor.dates import CycleWindow, add_months, is_expired, local_date
from tests.helpers import utc

WAW = ZoneInfo("Europe/Warsaw")


def test_architecture_example():
    """A§2: cykl z 04.11.2026 → przyjazdy 04.11.2026–03.02.2027, 92 daty."""
    window = CycleWindow.for_cycle(date(2026, 11, 4), 3)
    dates = window.checkin_dates()
    assert len(dates) == 92
    assert dates[0] == date(2026, 11, 4)
    assert dates[-1] == date(2027, 2, 3)
    stays = list(window.stays([2, 3, 4]))
    assert len(stays) == 92 * 3
    assert stays[-1] == (date(2027, 2, 3), date(2027, 2, 7), 4)


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 11, 30), 3, date(2027, 2, 28)),  # brak 30 lutego → ostatni dzień
        (date(2027, 11, 30), 3, date(2028, 2, 29)),  # rok przestępny
        (date(2026, 8, 31), 1, date(2026, 9, 30)),
        (date(2026, 10, 31), 3, date(2027, 1, 31)),  # przełom roku
        (date(2026, 12, 15), 3, date(2027, 3, 15)),
    ],
)
def test_add_months_clamps(start, months, expected):
    assert add_months(start, months) == expected


def test_window_has_no_gaps_or_duplicates_over_month_and_year_end():
    window = CycleWindow.for_cycle(date(2026, 12, 30), 3)
    dates = window.checkin_dates()
    assert dates[0] == date(2026, 12, 30)
    assert dates[-1] == date(2027, 3, 29)
    assert len(set(dates)) == len(dates) == (date(2027, 3, 30) - date(2026, 12, 30)).days


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # zmiana czasu na letni 28.03.2027 (CET → CEST)
        (utc(2027, 3, 27, 23, 30), date(2027, 3, 28)),
        (utc(2027, 3, 28, 21, 59), date(2027, 3, 28)),
        (utc(2027, 3, 28, 22, 0), date(2027, 3, 29)),
        # zmiana na zimowy 31.10.2027
        (utc(2027, 10, 30, 21, 59), date(2027, 10, 30)),
        (utc(2027, 10, 30, 22, 0), date(2027, 10, 31)),
        (utc(2027, 10, 31, 22, 59), date(2027, 10, 31)),
        (utc(2027, 10, 31, 23, 0), date(2027, 11, 1)),
    ],
)
def test_local_date_across_dst(moment, expected):
    assert local_date(moment, WAW) == expected


def test_window_spanning_dst_has_every_day():
    window = CycleWindow.for_cycle(date(2027, 3, 1), 1)
    assert len(window) == 31
    assert date(2027, 3, 28) in window.checkin_dates()


def test_is_expired_uses_warsaw_date():
    # 23:30 UTC 4.11 to już 5.11 w Warszawie
    now = utc(2026, 11, 4, 23, 30)
    assert is_expired(date(2026, 11, 4), now, WAW)
    assert not is_expired(date(2026, 11, 5), now, WAW)


def test_naive_datetime_rejected():
    from datetime import datetime

    with pytest.raises(ValueError):
        local_date(datetime(2026, 11, 4, 12), WAW)

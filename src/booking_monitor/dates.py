"""Okno dat cyklu (ARCHITEKTURA.md §2).

cycle_date    = data rozpoczęcia cyklu w strefie raportu (Europe/Warsaw)
end_exclusive = cycle_date + N miesięcy kalendarzowych (z przycięciem do końca miesiąca)
checkin       ∈ [cycle_date, end_exclusive)
checkout      = checkin + nights
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None:
        raise ValueError("oczekiwano czasu ze strefą (UTC)")


def add_months(day: date, months: int) -> date:
    """Przesunięcie o miesiące; nieistniejący dzień → ostatni dzień miesiąca docelowego."""
    return day + relativedelta(months=months)


def local_date(moment: datetime, tz: ZoneInfo) -> date:
    """Data kalendarzowa chwili `moment` w strefie `tz`."""
    _require_aware(moment)
    return moment.astimezone(tz).date()


def is_expired(checkin: date, now: datetime, tz: ZoneInfo) -> bool:
    """Data przyjazdu jest przeszła, jeśli wypada przed dzisiejszą datą w strefie raportu."""
    return checkin < local_date(now, tz)


@dataclass(frozen=True)
class CycleWindow:
    start: date
    end_exclusive: date

    @classmethod
    def for_cycle(cls, cycle_date: date, horizon_months: int) -> CycleWindow:
        if horizon_months < 1:
            raise ValueError("horyzont musi obejmować co najmniej 1 miesiąc")
        return cls(start=cycle_date, end_exclusive=add_months(cycle_date, horizon_months))

    def checkin_dates(self) -> list[date]:
        days = (self.end_exclusive - self.start).days
        return [self.start + timedelta(days=i) for i in range(days)]

    def __len__(self) -> int:
        return (self.end_exclusive - self.start).days

    def stays(self, nights: Sequence[int]) -> Iterator[tuple[date, date, int]]:
        """(checkin, checkout, nights) dla każdej daty przyjazdu i długości pobytu."""
        for checkin in self.checkin_dates():
            for n in sorted(nights):
                yield checkin, checkin + timedelta(days=n), n

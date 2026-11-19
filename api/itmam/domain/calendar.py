"""Working-day calendar and durations such as "2wd" (working days), "5d" or "12h"."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal

# The UAE has no daylight saving time, so a fixed offset is exact.
UAE_TZ = timezone(timedelta(hours=4), "UAE")

# Python weekday numbers: Monday = 0 ... Saturday = 5, Sunday = 6.
UAE_WEEKEND = frozenset({5, 6})


@dataclass(frozen=True)
class WorkCalendar:
    """Decides which days count as working days, in local (UAE) time."""

    weekend: frozenset[int] = UAE_WEEKEND
    holidays: frozenset[date] = field(default_factory=frozenset)
    tz: timezone = UAE_TZ

    def is_working_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend and day not in self.holidays

    def add_working_days(self, start: datetime, days: int) -> datetime:
        """Move forward `days` working days, keeping the local time of day.

        Starting on a weekend counts from the next working day, so Saturday + 1wd = Monday.
        """
        if days < 0:
            raise ValueError("working days must be zero or more")
        local = start.astimezone(self.tz)
        added = 0
        while added < days:
            local += timedelta(days=1)
            if self.is_working_day(local.date()):
                added += 1
        return local.astimezone(start.tzinfo)


UAE_CALENDAR = WorkCalendar()

DurationUnit = Literal["wd", "d", "h"]
_DURATION = re.compile(r"^(?P<amount>\d+)(?P<unit>wd|d|h)$")


@dataclass(frozen=True, order=True)
class Duration:
    """A clock length from pathway configuration: working days, calendar days or hours."""

    amount: int
    unit: DurationUnit

    @classmethod
    def parse(cls, text: str | Duration) -> Duration:
        if isinstance(text, Duration):
            return text
        match = _DURATION.match(str(text).strip())
        if not match:
            raise ValueError(f"not a duration: {text!r} (use e.g. 2wd, 5d or 12h)")
        unit: DurationUnit = match["unit"]  # type: ignore[assignment]
        return cls(int(match["amount"]), unit)

    def add_to(self, start: datetime, calendar: WorkCalendar = UAE_CALENDAR) -> datetime:
        if self.unit == "h":
            return start + timedelta(hours=self.amount)
        if self.unit == "d":
            return start + timedelta(days=self.amount)
        return calendar.add_working_days(start, self.amount)

    def approx_days(self) -> float:
        """Rough length in calendar days, only for sanity checks such as warn < breach."""
        if self.unit == "h":
            return self.amount / 24
        if self.unit == "d":
            return float(self.amount)
        return self.amount * 7 / 5

    def __str__(self) -> str:
        return f"{self.amount}{self.unit}"

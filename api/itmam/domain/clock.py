"""Clocks. The engine never reads the system time directly, so tests and demo sandboxes
can move time forward on purpose."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware, in UTC."""
        ...


class SystemClock:
    """Real time, for production."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to: used by tests and per-visitor demo sandboxes."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock needs a timezone-aware start time")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("time only moves forward")
        self._now += delta
        return self._now

    def set(self, moment: datetime) -> datetime:
        moment = moment.astimezone(UTC)
        if moment < self._now:
            raise ValueError("time only moves forward")
        self._now = moment
        return self._now

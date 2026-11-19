from datetime import UTC, date, datetime, timedelta

import pytest

from itmam.domain.calendar import UAE_CALENDAR, Duration, WorkCalendar


def uae(y: int, m: int, d: int, h: int = 10) -> datetime:
    """A UAE local time expressed in UTC (UAE = UTC+4)."""
    return datetime(y, m, d, h - 4, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "amount", "unit"),
    [("2wd", 2, "wd"), ("5d", 5, "d"), ("12h", 12, "h"), (" 7d ", 7, "d")],
)
def test_parse_durations(text: str, amount: int, unit: str) -> None:
    assert Duration.parse(text) == Duration(amount, unit)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["", "2", "2w", "-1d", "1.5d", "wd"])
def test_reject_bad_durations(text: str) -> None:
    with pytest.raises(ValueError):
        Duration.parse(text)


def test_friday_plus_one_working_day_is_monday() -> None:
    friday = uae(2026, 10, 23)
    assert Duration.parse("1wd").add_to(friday) == uae(2026, 10, 26)


def test_weekend_start_counts_from_next_working_day() -> None:
    saturday = uae(2026, 10, 24)
    assert UAE_CALENDAR.add_working_days(saturday, 1) == uae(2026, 10, 26)


def test_working_days_use_uae_local_dates() -> None:
    # Friday 23:30 UTC is already Saturday 03:30 in the UAE.
    late_friday_utc = datetime(2026, 10, 23, 23, 30, tzinfo=UTC)
    monday_local = UAE_CALENDAR.add_working_days(late_friday_utc, 1)
    assert monday_local.astimezone(UAE_CALENDAR.tz).date() == date(2026, 10, 26)


def test_holidays_are_skipped() -> None:
    calendar = WorkCalendar(holidays=frozenset({date(2026, 10, 20)}))
    monday = uae(2026, 10, 19)
    assert calendar.add_working_days(monday, 1) == uae(2026, 10, 21)


def test_calendar_and_hour_durations() -> None:
    start = uae(2026, 10, 19)
    assert Duration.parse("5d").add_to(start) == start + timedelta(days=5)
    assert Duration.parse("12h").add_to(start) == start + timedelta(hours=12)


def test_approximate_days_order_mixed_units() -> None:
    assert Duration.parse("3d").approx_days() < Duration.parse("5d").approx_days()
    assert Duration.parse("1wd").approx_days() < Duration.parse("2d").approx_days()

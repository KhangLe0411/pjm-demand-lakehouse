from datetime import date

import pytest

from src.features.calendar_features import (
    calendar_features,
    days_to_nearest_holiday,
    holiday_name,
    nerc_holidays,
)


@pytest.mark.parametrize("d,name", [
    (date(2024, 1, 1),  "new_year"),
    (date(2024, 5, 27), "memorial"),        # last Monday in May 2024
    (date(2024, 7, 4),  "independence"),
    (date(2024, 9, 2),  "labor"),           # first Monday in September 2024
    (date(2024, 11, 28), "thanksgiving"),   # 4th Thursday in November 2024
    (date(2024, 12, 25), "christmas"),
    (date(2026, 5, 25), "memorial"),
    (date(2026, 9, 7),  "labor"),
    (date(2026, 11, 26), "thanksgiving"),
])
def test_known_holidays(d, name):
    assert holiday_name(d) == name


def test_exactly_six_per_year():
    for y in range(2019, 2027):
        assert len(nerc_holidays(y)) == 6


def test_memorial_day_is_always_a_monday_in_may():
    for y in range(2019, 2027):
        d = _find(y, "memorial")
        assert d.weekday() == 0 and d.month == 5
        assert (d + __import__("datetime").timedelta(days=7)).month == 6   # last one


def test_thanksgiving_is_always_the_fourth_thursday():
    for y in range(2019, 2027):
        d = _find(y, "thanksgiving")
        assert d.weekday() == 3 and d.month == 11
        assert 22 <= d.day <= 28


def _find(year, name):
    return next(d for d, n in nerc_holidays(year).items() if n == name)


def test_ordinary_day_is_not_a_holiday():
    assert holiday_name(date(2024, 3, 14)) is None


@pytest.mark.parametrize("d,want", [
    (date(2024, 11, 28), 0),    # Thanksgiving itself
    (date(2024, 11, 29), 1),    # the Friday after - behaves like a holiday
    (date(2024, 11, 27), -1),
    (date(2024, 12, 27), 2),    # inside the Christmas-New Year lull
])
def test_days_to_nearest_holiday_is_signed(d, want):
    assert days_to_nearest_holiday(d) == want


def test_distance_crosses_the_year_boundary():
    """31 Dec is one day before New Year, not ~360 days after Christmas of its own year."""
    assert days_to_nearest_holiday(date(2024, 12, 31)) == -1


def test_calendar_features_shape():
    f = calendar_features(date(2024, 7, 4))
    assert f["is_holiday"] is True and f["holiday_name"] == "independence"
    assert f["day_of_week"] == 4 and f["month"] == 7 and f["day_of_year"] == 186
    assert f["is_weekend"] is False

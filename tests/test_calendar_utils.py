"""DST is the landmine in this pipeline, so it gets tested before anything reads it."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.silver.calendar_utils import expected_hours, local_day_bounds, local_hours

SPRING_2023, FALL_2023 = date(2023, 3, 12), date(2023, 11, 5)
SPRING_2026, FALL_2026 = date(2026, 3, 8), date(2026, 11, 1)


@pytest.mark.parametrize("d,want", [
    (SPRING_2023, 23), (FALL_2023, 25),
    (SPRING_2026, 23), (FALL_2026, 25),
    (date(2023, 7, 1), 24), (date(2026, 1, 15), 24),
])
def test_expected_hours(d, want):
    assert expected_hours(d) == want


def test_no_year_has_only_24_hour_days():
    """Guards the exact bug of hard-coding 24: a year must contain a 23 and a 25."""
    lens = {expected_hours(date(2024, 1, 1) + timedelta(days=i)) for i in range(366)}
    assert lens == {23, 24, 25}


def test_local_hours_length_matches_expected_hours():
    for d in (SPRING_2023, FALL_2023, date(2023, 7, 1)):
        assert len(local_hours(d)) == expected_hours(d)


def test_fall_back_yields_two_distinct_utc_instants_for_one_local_hour():
    """The 01:00 local hour happens twice. As UTC they are different rows."""
    eastern = ZoneInfo("America/New_York")
    ones = [h.astimezone(eastern) for h in local_hours(FALL_2023)]
    at_one = [h for h in ones if h.hour == 1]
    assert len(at_one) == 2
    assert at_one[0].utcoffset() != at_one[1].utcoffset()   # EDT then EST
    assert len({h.astimezone(ZoneInfo("UTC")) for h in at_one}) == 2


def test_spring_forward_skips_local_two_am():
    eastern = ZoneInfo("America/New_York")
    hours = {h.astimezone(eastern).hour for h in local_hours(SPRING_2026)}
    assert 2 not in hours
    assert 1 in hours and 3 in hours


def test_days_are_contiguous_across_a_dst_boundary():
    """No gap and no overlap where one operating day ends and the next begins."""
    for d in (SPRING_2026, FALL_2026):
        _, end = local_day_bounds(d)
        nxt_start, _ = local_day_bounds(d + timedelta(days=1))
        assert end == nxt_start

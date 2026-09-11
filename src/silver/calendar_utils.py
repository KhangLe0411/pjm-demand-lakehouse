"""Calendar helpers for a balancing authority's local operating day.

A local operating day is 23, 24 or 25 hours long. Every completeness check and every
peak-detection window has to read its length from the calendar rather than assume 24
(README sections 5 and 16 both assume 24 and are wrong twice a year).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")
PJM_TZ = "America/New_York"


def local_day_bounds(local_date: date, tz: str = PJM_TZ) -> tuple[datetime, datetime]:
    """UTC instants of local midnight on `local_date` and on the following day.

    The subtraction trap: two aware datetimes sharing one ZoneInfo subtract by
    WALL CLOCK, not elapsed time, so both ends must be converted to UTC first.
    Skipping that makes every DST day report 24 hours.
    """
    zone = ZoneInfo(tz)
    nxt = local_date + timedelta(days=1)
    start = datetime(local_date.year, local_date.month, local_date.day, tzinfo=zone)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def expected_hours(local_date: date, tz: str = PJM_TZ) -> int:
    """Hours in a local operating day: 23 on spring-forward, 25 on fall-back, else 24."""
    start_utc, end_utc = local_day_bounds(local_date, tz)
    return int((end_utc - start_utc).total_seconds() // 3600)


def local_hours(local_date: date, tz: str = PJM_TZ) -> list[datetime]:
    """Every UTC instant belonging to the local operating day, in order.

    Length matches `expected_hours`. On a fall-back day the two 01:00 local instants
    appear as two distinct UTC values, which is exactly why UTC is the canonical key.
    """
    start_utc, end_utc = local_day_bounds(local_date, tz)
    out, cur = [], start_utc
    while cur < end_utc:
        out.append(cur)
        cur += timedelta(hours=1)
    return out


def calendar_frame(spark, start: date, end: date, tz: str = PJM_TZ):
    """A date dimension carrying each local operating day's true length.

    Built in Python and joined, rather than computed with a UDF, because the length
    depends on tz database rules that Spark's own date functions do not expose.
    """
    days, cur = [], start
    while cur <= end:
        days.append((cur, expected_hours(cur, tz),
                     cur.weekday() >= 5, cur.isoweekday(), cur.month, cur.year))
        cur += timedelta(days=1)
    return spark.createDataFrame(
        days, ["local_date", "expected_hours", "is_weekend",
               "day_of_week", "month", "year"])

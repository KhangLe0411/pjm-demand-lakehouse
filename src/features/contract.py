"""The day-ahead forecast contract, in one place.

    cutoff  C = 10:00 local on forecast date D
    targets   every hour of the local operating day D+1  (23, 24 or 25 of them)
    horizon   ~14h to ~38h

Both ends are derived from the tz-aware calendar. The cutoff is a local wall-clock time,
so its UTC instant shifts by an hour across DST; the target day likewise is not always
24 hours. Hard-coding either produces a contract that is wrong twice a year.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.silver.calendar_utils import PJM_TZ, local_hours

CUTOFF_LOCAL_HOUR = 10


def cutoff_utc(forecast_date: date, tz: str = PJM_TZ) -> datetime:
    zone = ZoneInfo(tz)
    local = datetime(forecast_date.year, forecast_date.month, forecast_date.day,
                     CUTOFF_LOCAL_HOUR, tzinfo=zone)
    return local.astimezone(ZoneInfo("UTC"))


def target_hours_utc(forecast_date: date, tz: str = PJM_TZ) -> list[datetime]:
    return local_hours(forecast_date + timedelta(days=1), tz)


def horizon_hours(cutoff: datetime, target: datetime) -> float:
    return (target - cutoff).total_seconds() / 3600

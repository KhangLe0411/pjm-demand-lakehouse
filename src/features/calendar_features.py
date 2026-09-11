"""NERC holiday calendar.

The six NERC holidays, not the full federal list: these are the ones with a measurable
effect on electricity load, and they are what load-forecasting practice uses. Rule-based
rather than a package dependency, so the rules are visible and testable.

Bridging matters as much as the day itself — the Friday after Thanksgiving and the week
between Christmas and New Year behave like holidays without being one — so
`days_to_nearest_holiday` is provided alongside the boolean.
"""
from __future__ import annotations

from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th `weekday` (Mon=0) of a month; n<0 counts back from the end."""
    if n > 0:
        d = date(year, month, 1)
        shift = (weekday - d.weekday()) % 7
        return d + timedelta(days=shift + 7 * (n - 1))
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    d = nxt - timedelta(days=1)
    shift = (d.weekday() - weekday) % 7
    return d - timedelta(days=shift + 7 * (-n - 1))


def nerc_holidays(year: int) -> dict[date, str]:
    return {
        date(year, 1, 1):                    "new_year",
        _nth_weekday(year, 5, 0, -1):        "memorial",       # last Monday in May
        date(year, 7, 4):                    "independence",
        _nth_weekday(year, 9, 0, 1):         "labor",          # first Monday in Sept
        _nth_weekday(year, 11, 3, 4):        "thanksgiving",   # 4th Thursday in Nov
        date(year, 12, 25):                  "christmas",
    }


def holiday_name(d: date) -> str | None:
    return nerc_holidays(d.year).get(d)


def days_to_nearest_holiday(d: date) -> int:
    """Signed distance in days to the closest NERC holiday: negative before, positive
    after. Captures bridging without needing a rule per bridge."""
    cands: list[date] = []
    for y in (d.year - 1, d.year, d.year + 1):
        cands.extend(nerc_holidays(y))
    nearest = min(cands, key=lambda h: abs((d - h).days))
    return (d - nearest).days


def calendar_features(d: date) -> dict:
    name = holiday_name(d)
    return {
        "day_of_week": d.isoweekday(),
        "month": d.month,
        "day_of_year": d.timetuple().tm_yday,
        "is_weekend": d.weekday() >= 5,
        "is_holiday": name is not None,
        "holiday_name": name,
        "days_to_nearest_holiday": days_to_nearest_holiday(d),
    }

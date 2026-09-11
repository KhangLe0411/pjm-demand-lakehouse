#!/usr/bin/env python3
"""Milestone 0 - source validation.

Answers the questions in README section 30 before any pipeline code exists,
because the answers decide the training window and the DQ thresholds.

Usage:
    export EIA_API_KEY=...
    python3 scripts/validate_sources.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

EIA_ROOT = "https://api.eia.gov/v2/electricity/rto/region-data"
PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
REGION = "PJM"
TZ = "America/New_York"

# One representative point per major PJM load zone. V1 feeds these to the model
# as separate features rather than pre-averaging them (see docs/decisions.md).
PJM_POINTS = {
    "chicago_comed": (41.88, -87.63),
    "philadelphia_peco": (39.95, -75.17),
    "washington_pepco": (38.90, -77.04),
    "baltimore_bge": (39.29, -76.61),
    "newark_pseg": (40.74, -74.17),
    "pittsburgh_duq": (40.44, -79.99),
    "cleveland_atsi": (41.50, -81.69),
    "richmond_dom": (37.54, -77.44),
}


def get_json(url: str, params: dict, timeout: int = 60) -> dict:
    qs = []
    for k, v in params.items():
        if isinstance(v, (list, tuple)):
            qs.extend((k, str(i)) for i in v)
        else:
            qs.append((k, str(v)))
    full = f"{url}?{urllib.parse.urlencode(qs)}"
    req = urllib.request.Request(full, headers={"User-Agent": "energy-lakehouse/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- EIA

def eia(path: str, key: str, **params) -> dict:
    params["api_key"] = key
    return get_json(f"{EIA_ROOT}{path}", params)


def q1_q3_facets(key: str) -> None:
    print("\n[Q1-Q3] PJM / D / DF ton tai trong facets?")
    for facet in ("respondent", "type"):
        try:
            body = eia(f"/facet/{facet}/", key)["response"]["facets"]
        except Exception as e:  # noqa: BLE001
            print(f"  {facet}: LOI {e}")
            continue
        ids = {f["id"] for f in body}
        if facet == "respondent":
            print(f"  respondent: {len(ids)} gia tri | PJM = {'CO' if REGION in ids else 'KHONG'}")
        else:
            for t in ("D", "DF", "NG", "TI"):
                print(f"  type {t:3s} = {'CO' if t in ids else 'KHONG':6s}", end="")
            print()


def q4_coverage(key: str) -> None:
    """Row count per year per type. Expected ~8760 (8784 in leap years)."""
    print(f"\n[Q4] Do phu lich su cua D vs DF cho {REGION}")
    print(f"  {'nam':6s} {'D':>8s} {'DF':>8s} {'ky vong':>8s}  {'DF %':>6s}")
    this_year = date.today().year
    for year in range(2015, this_year + 1):
        counts = {}
        for typ in ("D", "DF"):
            try:
                body = eia(
                    "/data/", key,
                    frequency="hourly",
                    **{"data[0]": "value",
                       "facets[respondent][]": REGION,
                       "facets[type][]": typ},
                    start=f"{year}-01-01T00",
                    end=f"{year}-12-31T23",
                    length=1,
                )["response"]
                counts[typ] = int(body.get("total", 0))
            except Exception as e:  # noqa: BLE001
                print(f"  {year}: LOI {typ} -> {e}")
                counts[typ] = 0
        leap = (year % 4 == 0 and year % 100 != 0) or year % 400 == 0
        expected = 8784 if leap else 8760
        pct = 100.0 * counts["DF"] / expected if expected else 0
        print(f"  {year:<6d} {counts['D']:>8d} {counts['DF']:>8d} {expected:>8d}  {pct:>5.1f}%")


def q6_thresholds(key: str) -> None:
    """Calibrate the hard sanity bounds from real data instead of guessing."""
    print(f"\n[Q6] Bien do demand thuc te (de dat hard bound) - mau 2023-2024")
    vals: list[float] = []
    offset = 0
    while offset < 20000:
        body = eia(
            "/data/", key,
            frequency="hourly",
            **{"data[0]": "value",
               "facets[respondent][]": REGION,
               "facets[type][]": "D",
               "sort[0][column]": "period",
               "sort[0][direction]": "asc"},
            start="2023-01-01T00", end="2024-12-31T23",
            length=5000, offset=offset,
        )["response"]
        rows = body.get("data", [])
        if not rows:
            break
        for r in rows:
            v = r.get("value")
            if v is not None:
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    pass
        offset += len(rows)
    if not vals:
        print("  khong lay duoc du lieu")
        return
    vals.sort()
    n = len(vals)
    pick = lambda p: vals[min(n - 1, int(p * n))]  # noqa: E731
    print(f"  n={n}  min={vals[0]:,.0f}  p01={pick(.01):,.0f}  p50={pick(.50):,.0f}"
          f"  p95={pick(.95):,.0f}  p99={pick(.99):,.0f}  max={vals[-1]:,.0f}  (MWh)")
    print(f"  -> de xuat hard bound: {int(vals[0] * 0.5):,} .. {int(vals[-1] * 1.35):,}")
    print(f"  -> nguong extreme regime (P95, se tinh lai theo fold): {pick(.95):,.0f}")


# ---------------------------------------------------------------- Weather

def q5_weather_window() -> None:
    """Binary-search the first date the previous-runs archive returns values."""
    print("\n[Q5] Bien duoi cua Open-Meteo Previous Runs (quyet dinh training window)")
    lat, lon = PJM_POINTS["philadelphia_peco"]

    def has_data(d: date) -> bool:
        try:
            body = get_json(PREV_RUNS, {
                "latitude": lat, "longitude": lon,
                "start_date": d.isoformat(), "end_date": d.isoformat(),
                "hourly": "temperature_2m_previous_day1", "timezone": "UTC",
            })
        except urllib.error.HTTPError:
            return False
        v = body.get("hourly", {}).get("temperature_2m_previous_day1", [])
        return bool(v) and any(x is not None for x in v)

    lo, hi = date(2020, 1, 1), date(2022, 1, 1)
    if has_data(lo):
        print(f"  co du lieu tu {lo} tro ve truoc - noi rong khoang tim")
        return
    while (hi - lo).days > 1:
        mid = lo + timedelta(days=(hi - lo).days // 2)
        (lo, hi) = (lo, mid) if has_data(mid) else (mid, hi)
    print(f"  ngay dau tien co du lieu: ~{hi.isoformat()}")
    days = (date.today() - hi).days
    print(f"  -> training window co weather: ~{days:,} ngay (~{days / 365.25:.1f} nam)")
    print(f"  -> training window KHONG weather (EIA tu 2015-07): "
          f"~{(date.today() - date(2015, 7, 1)).days:,} ngay")


def q7_dst() -> None:
    """A local operating day is not always 24 hours. Prove it, then encode it.

    Note the trap: subtracting two aware datetimes that share the same ZoneInfo
    gives WALL-CLOCK arithmetic, not elapsed time. Both must be converted to UTC
    first, otherwise every DST day silently reports 24 hours.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        print("  can Python 3.9+")
        return
    print("\n[Q7] So gio cua mot ngay van hanh local (canh bao cho completeness check)")
    tz, utc = ZoneInfo(TZ), ZoneInfo("UTC")
    for d in (date(2023, 3, 12), date(2023, 7, 1), date(2023, 11, 5),
              date(2026, 3, 8), date(2026, 11, 1)):
        nd = d + timedelta(days=1)
        a = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(utc)
        b = datetime(nd.year, nd.month, nd.day, tzinfo=tz).astimezone(utc)
        hours = int((b - a).total_seconds() // 3600)
        flag = "" if hours == 24 else f"   <-- {hours} gio, KHONG PHAI 24"
        print(f"  {d}  ->  {hours} gio{flag}")


def main() -> int:
    key = os.environ.get("EIA_API_KEY")
    q5_weather_window()
    q7_dst()
    if not key:
        print("\n!! EIA_API_KEY chua set - bo qua Q1-Q4, Q6.")
        print("   Lay key mien phi: https://www.eia.gov/opendata/register.php")
        return 1
    q1_q3_facets(key)
    q4_coverage(key)
    q6_thresholds(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())

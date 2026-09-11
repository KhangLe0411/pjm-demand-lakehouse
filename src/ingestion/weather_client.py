#!/usr/bin/env python3
"""Archived weather *forecasts* for the PJM footprint -> Parquet landing zone.

Source is Open-Meteo's **Previous Runs** API, not the Historical Forecast API. The
latter stitches together short lead-time forecasts, so for a target 14-38 hours past
the cutoff it is far more accurate than anything that existed at the cutoff and would
leak while passing every check in README section 8 (docs/decisions.md D-01).

`forecast_lead_days` is the authoritative provenance field, and both available leads are
ingested rather than one:

  lead 2  the run from D-1. For a 10:00 ET cutoff on day D, any run on D-1 is
          *provably* earlier than the cutoff. This is the safe primary.
  lead 1  the run from day D. Very likely earlier than 10:00 ET, but Open-Meteo does
          not expose the archived run hour, so it cannot be proven. Carried as the
          fresher variant; the accuracy gap between the two is the measured price of
          the safety margin rather than an assumption.

Usage:
    python -m src.ingestion.weather_client --mode backfill
    python -m src.ingestion.weather_client --mode probe
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute
import pyarrow.parquet as pq

API = "https://previous-runs-api.open-meteo.com/v1/forecast"
REGION = "PJM"

# Measured floors, see docs/decisions.md D-21. Temperature reaches back roughly twice
# as far as everything else, which is why the feature set is not uniform.
TEMP_START = date(2021, 3, 24)
RICH_START = date(2024, 1, 19)

LEADS = (1, 2)

# api variable stem -> output column
VARIABLES = {
    "temperature_2m":        "temperature_c",
    "apparent_temperature":  "apparent_temperature_c",
    "dew_point_2m":          "dew_point_c",
    "relative_humidity_2m":  "relative_humidity_pct",
    "wind_speed_10m":        "wind_speed_kmh",
    "precipitation":         "precipitation_mm",
}

# One representative point per major PJM load zone. PJM spans ~13 states, so a single
# coordinate is not representative. These are fed to the model as separate features
# rather than pre-averaged: population weighting needs weights that must themselves be
# justified, and letting the model learn the relative importance avoids inventing them
# (docs/decisions.md D-08).
PJM_POINTS: dict[str, tuple[float, float]] = {
    "chicago_comed":      (41.88, -87.63),
    "philadelphia_peco":  (39.95, -75.17),
    "washington_pepco":   (38.90, -77.04),
    "baltimore_bge":      (39.29, -76.61),
    "newark_pseg":        (40.74, -74.17),
    "pittsburgh_duq":     (40.44, -79.99),
    "cleveland_atsi":     (41.50, -81.69),
    "richmond_dom":       (37.54, -77.44),
}

SCHEMA = pa.schema([
    ("valid_timestamp_utc", pa.string()),
    ("forecast_lead_days", pa.int32()),
    ("region_id", pa.string()),
    ("point_id", pa.string()),
    ("latitude", pa.float64()),
    ("longitude", pa.float64()),
    *[(c, pa.float64()) for c in VARIABLES.values()],
    ("source", pa.string()),
    ("source_file", pa.string()),
    ("ingested_at", pa.string()),
])


class WeatherError(RuntimeError):
    pass


def _hourly_params() -> list[str]:
    return [f"{stem}_previous_day{lead}" for lead in LEADS for stem in VARIABLES]


def fetch(start: date, end: date, max_retries: int = 5, timeout: int = 180) -> list[dict]:
    """One call covers every point: the API accepts comma-separated coordinates and
    returns a JSON array, which cuts the request count by the number of points."""
    lats = ",".join(str(PJM_POINTS[p][0]) for p in PJM_POINTS)
    lons = ",".join(str(PJM_POINTS[p][1]) for p in PJM_POINTS)
    q = urllib.parse.urlencode({
        "latitude": lats, "longitude": lons,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": ",".join(_hourly_params()),
        "timezone": "UTC",   # never a named zone: Open-Meteo normalises DST days to 24
                             # rows under a local zone, hiding the discontinuity (D-03)
    })
    url = f"{API}?{q}"
    delay = 2.0
    last: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "energy-demand-lakehouse/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode())
            payload = body if isinstance(body, list) else [body]
            if payload and "reason" in payload[0]:
                raise WeatherError(str(payload[0]["reason"])[:300])
            return payload
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as e:
            last = e
            print(f"    {type(e).__name__} (attempt {attempt}/{max_retries}) "
                  f"retry in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise WeatherError(f"exhausted {max_retries} retries: {last}")


def to_columns(payload: list[dict], run_ts: datetime,
               source_file: str) -> dict[str, list]:
    """Flatten {location -> {hourly -> {var_previous_dayN -> [...]}}} to long rows
    keyed by (valid timestamp, point, lead)."""
    out: dict[str, list] = {name: [] for name in SCHEMA.names}
    stamp = run_ts.isoformat()
    ids = list(PJM_POINTS)
    for idx, loc in enumerate(payload):
        point_id = ids[idx] if idx < len(ids) else f"unknown_{idx}"
        hourly = loc.get("hourly") or {}
        times = hourly.get("time") or []
        for lead in LEADS:
            series = {col: hourly.get(f"{stem}_previous_day{lead}") or []
                      for stem, col in VARIABLES.items()}
            for i, t in enumerate(times):
                out["valid_timestamp_utc"].append(t)
                out["forecast_lead_days"].append(lead)
                out["region_id"].append(REGION)
                out["point_id"].append(point_id)
                out["latitude"].append(loc.get("latitude"))
                out["longitude"].append(loc.get("longitude"))
                for col in VARIABLES.values():
                    vals = series[col]
                    out[col].append(vals[i] if i < len(vals) else None)
                out["source"].append("open-meteo/previous-runs-api")
                out["source_file"].append(source_file)
                out["ingested_at"].append(stamp)
    return out


def write_landing(cols: dict[str, list], root: Path, run_ts: datetime) -> int:
    """One Parquet file per valid date, named by run, mirroring the EIA extractor:
    a re-fetch adds a vintage instead of overwriting one."""
    table = pa.table(cols, schema=SCHEMA)
    dates = {t[:10] for t in cols["valid_timestamp_utc"]}
    tag = run_ts.strftime("%Y%m%dT%H%M%SZ")
    written = 0
    for day in sorted(dates):
        mask = pa.compute.starts_with(table.column("valid_timestamp_utc"), pattern=day)
        part = table.filter(mask)
        if part.num_rows == 0:
            continue
        d = root / "weather" / f"region={REGION}" / f"date={day}"
        d.mkdir(parents=True, exist_ok=True)
        pq.write_table(part, d / f"part-{tag}.parquet", compression="snappy")
        written += 1
    return written


def month_chunks(start: date, end: date, months: int = 6):
    cur = start
    while cur <= end:
        stop = cur
        for _ in range(months):
            stop = (stop.replace(day=1) + timedelta(days=32)).replace(day=1)
        stop = min(stop - timedelta(days=1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("backfill", "probe"), default="backfill")
    ap.add_argument("--start", help="YYYY-MM-DD (default = temperature archive floor)")
    ap.add_argument("--end", help="YYYY-MM-DD (default = yesterday UTC)")
    ap.add_argument("--out", default="data/landing")
    a = ap.parse_args()

    if a.mode == "probe":
        payload = fetch(date(2024, 7, 1), date(2024, 7, 1))
        loc = payload[0]
        print(f"locations returned: {len(payload)}")
        print(f"point 0: lat={loc['latitude']} lon={loc['longitude']}")
        for k, v in (loc.get("hourly") or {}).items():
            if k == "time":
                continue
            nulls = sum(x is None for x in v)
            print(f"  {k:<46} n={len(v):>4} nulls={nulls}")
        return 0

    start = date.fromisoformat(a.start) if a.start else TEMP_START
    # The archive lags real time; yesterday is the last date with a complete day.
    end = (date.fromisoformat(a.end) if a.end
           else datetime.now(timezone.utc).date() - timedelta(days=1))
    start = max(start, TEMP_START)
    run_ts = datetime.now(timezone.utc).replace(microsecond=0)

    print(f"points={len(PJM_POINTS)} leads={LEADS} vars={len(VARIABLES)}")
    print(f"khoang {start} -> {end}   run_ts={run_ts.isoformat()}")
    print(f"luu y: cac bien ngoai temperature chi co tu {RICH_START} (D-21)")
    total_rows = total_files = 0
    for c_start, c_end in month_chunks(start, end):
        payload = fetch(c_start, c_end)
        cols = to_columns(payload, run_ts, f"weather/{c_start}_{c_end}.parquet")
        n = write_landing(cols, Path(a.out), run_ts)
        rows = len(cols["valid_timestamp_utc"])
        total_rows += rows
        total_files += n
        print(f"  {c_start} .. {c_end}   rows={rows:>8,}  files={n:>4}")
    print(f"\nTONG rows={total_rows:,}  files={total_files:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

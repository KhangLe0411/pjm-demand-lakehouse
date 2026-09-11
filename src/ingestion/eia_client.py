#!/usr/bin/env python3
"""EIA-930 extractor: API -> Parquet in the ADLS landing zone.

Bronze is raw and append-only (README sections 12-13), which drives two rules here:

  1. `value` is written as the STRING the API returned. Casting is Silver's job, so a
     malformed value lands and is quarantined rather than crashing ingestion.
  2. Every run stamps a fresh `ingested_at` and writes a distinct filename, so
     re-fetching a period that upstream has revised produces a NEW file rather than
     overwriting. Auto Loader sees a new file, Bronze accumulates both vintages, and
     Silver resolves them by `ingested_at`. Overwriting here would destroy exactly the
     signal the project sets out to measure.

Usage:
    python -m src.ingestion.eia_client --mode backfill
    python -m src.ingestion.eia_client --mode incremental --lookback-days 14
    python -m src.ingestion.eia_client --mode probe
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

API = "https://api.eia.gov/v2/electricity/rto/region-data/data/"
PAGE = 5000                      # EIA hard-caps `length` at 5000
ROUTE_START = date(2019, 1, 1)   # measured; see docs/decisions.md D-02
DEFAULT_TYPES = ("D", "DF", "NG", "TI")


class EIAError(RuntimeError):
    pass


class EIAClient:
    def __init__(self, api_key: str, respondent: str = "PJM",
                 max_retries: int = 5, timeout: int = 60) -> None:
        if not api_key:
            raise EIAError("EIA_API_KEY is empty")
        self.api_key = api_key
        self.respondent = respondent
        self.max_retries = max_retries
        self.timeout = timeout

    # -------------------------------------------------------------- transport

    def _get(self, params: list[tuple[str, str]]) -> dict:
        """GET with exponential backoff. Retries 429/5xx and transport errors."""
        url = f"{API}?{urllib.parse.urlencode(params)}"
        delay = 1.0
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "energy-demand-lakehouse/0.1"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 504):
                    body = e.read()[:200].decode(errors="replace")
                    print(f"    HTTP {e.code} (attempt {attempt}/{self.max_retries}) "
                          f"retry in {delay:.0f}s :: {body}", file=sys.stderr)
                else:
                    raise EIAError(f"HTTP {e.code}: {e.read()[:300]!r}") from e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last = e
                print(f"    {type(e).__name__} (attempt {attempt}/{self.max_retries}) "
                      f"retry in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 60)
        raise EIAError(f"exhausted {self.max_retries} retries: {last}")

    def _params(self, start: str, end: str, types: tuple[str, ...],
                offset: int, length: int) -> list[tuple[str, str]]:
        p = [
            ("api_key", self.api_key),
            ("frequency", "hourly"),
            ("data[0]", "value"),
            ("facets[respondent][]", self.respondent),
            ("start", start), ("end", end),
            ("sort[0][column]", "period"), ("sort[0][direction]", "asc"),
            ("offset", str(offset)), ("length", str(length)),
        ]
        p.extend(("facets[type][]", t) for t in types)
        return p

    # ---------------------------------------------------------------- fetching

    def fetch(self, start: str, end: str,
              types: tuple[str, ...] = DEFAULT_TYPES) -> list[dict]:
        """Fetch one period window, following pagination to completion."""
        rows: list[dict] = []
        offset = 0
        total: int | None = None
        while True:
            body = self._get(self._params(start, end, types, offset, PAGE))
            resp = body.get("response") or {}
            if total is None:
                total = int(resp.get("total", 0))
            batch = resp.get("data") or []
            if not batch:
                break
            rows.extend(batch)
            offset += len(batch)
            if offset >= total or len(batch) < PAGE:
                break
        if total is not None and len(rows) != total:
            print(f"    canh bao: nhan {len(rows)} != total {total} cho {start}..{end}",
                  file=sys.stderr)
        return rows


# ------------------------------------------------------------------- landing IO

def normalise(rows: list[dict], run_ts: datetime, source_file: str) -> dict[str, list]:
    """Hyphenated API keys -> snake_case. `value` deliberately stays a string."""
    out: dict[str, list] = {
        "period": [], "respondent": [], "respondent_name": [],
        "type": [], "type_name": [], "value": [], "value_units": [],
        "source": [], "source_file": [], "ingested_at": [],
    }
    stamp = run_ts.isoformat()
    for r in rows:
        out["period"].append(r.get("period"))
        out["respondent"].append(r.get("respondent"))
        out["respondent_name"].append(r.get("respondent-name") or r.get("respondent_name"))
        out["type"].append(r.get("type"))
        out["type_name"].append(r.get("type-name") or r.get("type_name"))
        v = r.get("value")
        out["value"].append(None if v is None else str(v))
        out["value_units"].append(r.get("value-units") or r.get("value_units"))
        out["source"].append("EIA-930/api-v2/electricity/rto/region-data")
        out["source_file"].append(source_file)
        out["ingested_at"].append(stamp)
    return out


SCHEMA = pa.schema([(k, pa.string()) for k in (
    "period", "respondent", "respondent_name", "type", "type_name",
    "value", "value_units", "source", "source_file", "ingested_at")])


def write_landing(rows: list[dict], root: Path, respondent: str,
                  run_ts: datetime) -> list[Path]:
    """One Parquet file per (period date, run). Never overwrites a prior vintage."""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        p = r.get("period") or ""
        by_day.setdefault(p[:10], []).append(r)

    tag = run_ts.strftime("%Y%m%dT%H%M%SZ")
    written: list[Path] = []
    for day, day_rows in sorted(by_day.items()):
        if not day:
            continue
        d = root / "eia" / "region-data" / f"respondent={respondent}" / f"date={day}"
        d.mkdir(parents=True, exist_ok=True)
        fname = f"part-{tag}.parquet"
        cols = normalise(day_rows, run_ts, f"{d.name}/{fname}")
        pq.write_table(pa.table(cols, schema=SCHEMA), d / fname, compression="snappy")
        written.append(d / fname)
    return written


def month_windows(start: date, end: date):
    """Chunk by calendar month: bounded page counts and restartable on failure."""
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        stop = min(nxt - timedelta(days=1), end)
        yield f"{cur.isoformat()}T00", f"{stop.isoformat()}T23"
        cur = nxt


# ------------------------------------------------------------------------ modes

def run_probe(client: EIAClient) -> None:
    """Dump raw records so the period timezone can be settled from evidence."""
    rows = client.fetch("2026-09-01T00", "2026-09-01T02", ("D", "DF"))
    print(json.dumps(rows[:4], indent=2))
    if rows:
        print("\nkeys:", sorted(rows[0].keys()))
        print("\nperiod dau/cuoi:", rows[0]["period"], "->", rows[-1]["period"])
        print("!! Xac nhan period la UTC hay local truoc khi viet Silver "
              "(docs/decisions.md D-03).")


def run_extract(client: EIAClient, start: date, end: date, root: Path,
                types: tuple[str, ...]) -> None:
    run_ts = datetime.now(timezone.utc).replace(microsecond=0)
    print(f"respondent={client.respondent}  types={','.join(types)}")
    print(f"khoang {start} -> {end}   run_ts={run_ts.isoformat()}")
    total_rows = total_files = 0
    for w_start, w_end in month_windows(start, end):
        rows = client.fetch(w_start, w_end, types)
        files = write_landing(rows, root, client.respondent, run_ts)
        total_rows += len(rows)
        total_files += len(files)
        print(f"  {w_start[:7]}  rows={len(rows):>6,}  files={len(files):>3}")
    print(f"\nTONG  rows={total_rows:,}  files={total_files:,}  ->  {root}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("backfill", "incremental", "probe"),
                    default="incremental")
    ap.add_argument("--respondent", default="PJM")
    ap.add_argument("--types", default=",".join(DEFAULT_TYPES))
    ap.add_argument("--start", help="YYYY-MM-DD (backfill; default = route start)")
    ap.add_argument("--end", help="YYYY-MM-DD (default = today UTC)")
    ap.add_argument("--lookback-days", type=int, default=14,
                    help="incremental: re-fetch this many days to capture revisions")
    ap.add_argument("--out", default="data/landing", help="landing root")
    a = ap.parse_args()

    key = os.environ.get("EIA_API_KEY", "")
    if not key:
        env = Path(__file__).resolve().parents[2] / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("EIA_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        print("EIA_API_KEY khong tim thay (env hoac .env)", file=sys.stderr)
        return 2

    client = EIAClient(key, a.respondent)
    if a.mode == "probe":
        run_probe(client)
        return 0

    today = datetime.now(timezone.utc).date()
    end = date.fromisoformat(a.end) if a.end else today
    if a.mode == "backfill":
        start = date.fromisoformat(a.start) if a.start else ROUTE_START
    else:
        start = end - timedelta(days=a.lookback_days)
    start = max(start, ROUTE_START)

    types = tuple(t.strip() for t in a.types.split(",") if t.strip())
    run_extract(client, start, end, Path(a.out), types)
    return 0


if __name__ == "__main__":
    sys.exit(main())

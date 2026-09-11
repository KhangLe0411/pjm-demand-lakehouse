#!/usr/bin/env python3
"""Winter Storm Elliott, 23-25 December 2022 — README section 20.

A stress test, not a demonstration that the model works. The questions section 20 asks
are answered from the backtest output, including the ones with unflattering answers.
"""
from __future__ import annotations

import sys

import pandas as pd

sys.path.insert(0, ".")
from src.ml import evaluate as ev  # noqa: E402
from src.ml.backtest import scoreable  # noqa: E402
from src.ml.models import LABEL  # noqa: E402

WINDOW = ("2022-12-20", "2022-12-28")
EVENT = ("2022-12-23", "2022-12-25")
pd.set_option("display.width", 170)


def banner(title: str) -> None:
    print("\n" + "=" * 86)
    print(title)
    print("=" * 86)

p = scoreable(pd.read_parquet("data/features/backtest_predictions.parquet"))
p["d"] = pd.to_datetime(p["target_local_date"])
win = p[(p.d >= WINDOW[0]) & (p.d <= WINDOW[1])]
ev_rows = p[(p.d >= EVENT[0]) & (p.d <= EVENT[1])]

banner("1. WHAT HAPPENED — daily actuals around the event")
day = (win[win.model == "eia_df"].groupby("d")
       .agg(peak_mwh=(LABEL, "max"), mean_mwh=(LABEL, "mean"),
            min_mwh=(LABEL, "min")).round(0))
base = p[(p.d >= "2022-12-01") & (p.d < "2022-12-20") & (p.model == "eia_df")]
dec_norm = base[LABEL].max()
day["peak_vs_dec_normal_pct"] = (100 * (day.peak_mwh - dec_norm) / dec_norm).round(1)
print(day.to_string())
print(f"\n  December 1-19 peak for reference: {dec_norm:,.0f} MWh")

banner("2. HOW EACH MODEL DID — event days only")
print(ev.overall(ev_rows).to_string(index=False))
print("\n  vs the same models over the whole backtest:")
print(ev.overall(p)[["model", "MAE_mwh", "MAPE_pct", "bias_mwh"]].to_string(index=False))

banner("3. DIRECTION — did anyone see the ramp coming?")
piv = (win.pivot_table(index="d", columns="model", values="error_mwh", aggfunc="mean")
       .round(0))
piv.insert(0, "actual_peak", day.peak_mwh)
print(piv.to_string())
print("\n  negative = under-forecast")

banner("4. PEAK behaviour on event days")
pk = ev.peak_metrics(ev_rows, require_full_day=False)
if not pk.empty:
    print(pk[["model", "target_local_date", "actual_peak_mwh", "actual_peak_hour",
              "pred_peak_mwh", "pred_peak_hour", "peak_magnitude_error_pct",
              "peak_timing_error_hours"]].round(2).to_string(index=False))

banner("5. WORST SINGLE HOURS of the event")
w = ev_rows.nlargest(8, "abs_error_mwh")
print(w[["model", "target_local_date", "target_local_hour", LABEL,
         "prediction_mwh", "error_mwh"]].round(0).to_string(index=False))

#!/usr/bin/env python3
"""What the provable-availability margin costs — the D-21 sensitivity.

The primary model uses lead-2 weather because a run from day D-1 is *provably* earlier
than a 10:00 cutoff on day D. Lead 1 is a run from day D itself, very likely earlier
but not demonstrably so, because Open-Meteo does not publish the archived run hour.

This measures the accuracy difference between the two. The lead-1 model is a
measurement instrument, not a candidate: `audit()` rejects its feature set, and this
script asserts that it still does before reporting a single number. A variant that the
audit refuses must never quietly become the headline.
"""
from __future__ import annotations

import sys
from datetime import date

import pandas as pd

sys.path.insert(0, ".")
from src.features.contract import cutoff_utc, target_hours_utc  # noqa: E402
from src.features.spec import (  # noqa: E402
                               FEATURES_LEAD1_VARIANT,
                               FEATURES_MODEL_A,
                               FEATURES_MODEL_B,
                               audit,
)
from src.ml import evaluate as ev  # noqa: E402
from src.ml.backtest import monthly_folds, run_backtest, scoreable  # noqa: E402
from src.ml.models import XGBModel, eia_benchmark  # noqa: E402

lead2 = [s.name for s in FEATURES_MODEL_B]
lead1 = [s.name for s in FEATURES_MODEL_A] + [s.name for s in FEATURES_LEAD1_VARIANT]

# Gate first, numbers second.
d = date(2024, 6, 20)
c, t = cutoff_utc(d), target_hours_utc(d)[0]
assert not audit(FEATURES_MODEL_B, c, t), "lead-2 set unexpectedly leaks"
v = audit(FEATURES_MODEL_A + FEATURES_LEAD1_VARIANT, c, t)
assert v, "lead-1 set unexpectedly PASSED the audit — re-check the run-instant bound"
print(f"leakage audit: lead-2 PASSES · lead-1 REJECTED ({len(v)} features)")
print(f"  e.g. {v[0]}\n")

df = pd.read_parquet("data/features/demand_features")
missing = [c_ for c_ in lead1 if c_ not in df.columns]
assert not missing, f"lead-1 columns absent — rerun export_features.py: {missing[:3]}"

folds = monthly_folds(df["forecast_date"], 12)
preds = run_backtest(df, [
    eia_benchmark(),
    XGBModel(name="xgb_b", features=lead2),
    XGBModel(name="xgb_b_lead1", features=lead1),
], folds, verbose=False)
s = scoreable(preds)
preds.to_parquet("data/features/backtest_lead1.parquet", index=False)

pd.set_option("display.width", 165)

def banner(title: str) -> None:
    print("\n" + "=" * 84)
    print(title)
    print("=" * 84)

banner("OVERALL")
print(ev.versus(s).to_string(index=False))
banner("PEAK")
print(ev.peak_summary(ev.peak_metrics(s)).to_string(index=False))
banner("EXTREME REGIME")
r = ev.by_regime(s)
print(r[r.regime == "extreme"].to_string(index=False))

o = ev.overall(s).set_index("model")
bench = o.loc["eia_df", "MAE_mwh"]
gap_l2 = 100 * (o.loc["xgb_b", "MAE_mwh"] - bench) / bench
gap_l1 = 100 * (o.loc["xgb_b_lead1", "MAE_mwh"] - bench) / bench
lead1 = o.loc["xgb_b_lead1", "MAE_mwh"]
cost = 100 * (o.loc["xgb_b", "MAE_mwh"] - lead1) / lead1
banner("WHAT THE SAFETY MARGIN COSTS")
print(f"  gap to benchmark, lead-2 (deployable) : {gap_l2:+.2f}% MAE")
print(f"  gap to benchmark, lead-1 (rejected)   : {gap_l1:+.2f}% MAE")
print(f"  price of provable availability        : {cost:+.2f}% MAE")

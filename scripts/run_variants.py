#!/usr/bin/env python3
"""Two pre-registered variants against the diagnosed cause, and nothing else.

The diagnosis was concept drift: PJM demand rose ~11.5% over the backtest window and
xgb_b's bias worsened from -497 to -1,995 MWh, correlating -0.585 with training-set
size. Each variant targets that specific mechanism:

  sliding window   caps how old the training data may be
  ratio target     models demand relative to a recent level, so the level cannot bias it

Both were named in the decision log before the numbers were seen. Running a sweep until
something wins would be fitting the backtest rather than the problem, so the list stops
here whatever the outcome.
"""
from __future__ import annotations

import sys

import pandas as pd

sys.path.insert(0, ".")
from src.features.spec import FEATURES_MODEL_B  # noqa: E402
from src.ml import evaluate as ev  # noqa: E402
from src.ml.backtest import monthly_folds, run_backtest, scoreable  # noqa: E402
from src.ml.models import XGBModel, XGBRatioModel, eia_benchmark  # noqa: E402

df = pd.read_parquet("data/features/demand_features")
feats = [s.name for s in FEATURES_MODEL_B]
WINDOW = 24

runs = [
    ("expanding", monthly_folds(df["forecast_date"], 12), [
        eia_benchmark(),
        XGBModel(name="xgb_b", features=feats),
        XGBRatioModel(name="xgb_b_ratio", features=feats)]),
    ("sliding24", monthly_folds(df["forecast_date"], 12, window_months=WINDOW), [
        XGBModel(name="xgb_b_slide24", features=feats),
        XGBRatioModel(name="xgb_b_ratio_slide24", features=feats)]),
]

out = []
for label, folds, models in runs:
    print(f"\n### {label}: {len(folds)} folds, {len(models)} models", flush=True)
    out.append(run_backtest(df, models, folds, verbose=False))
    print("    done", flush=True)

preds = pd.concat(out, ignore_index=True)
preds.to_parquet("data/features/backtest_variants.parquet", index=False)
s = scoreable(preds)

pd.set_option("display.width", 165)

def banner(title: str) -> None:
    print("\n" + "=" * 84)
    print(title)
    print("=" * 84)

banner("OVERALL")
print(ev.versus(s).to_string(index=False))
banner("BIAS BY YEAR  (pred - actual, MWh)")
s2 = s.copy()
s2["year"] = pd.to_datetime(s2.forecast_date).dt.year
print(s2.pivot_table(index="year", columns="model", values="error_mwh",
                     aggfunc="mean").round(0).to_string())
banner("PEAK")
print(ev.peak_summary(ev.peak_metrics(s)).to_string(index=False))
banner("EXTREME REGIME")
r = ev.by_regime(s)
print(r[r.regime == "extreme"].to_string(index=False))

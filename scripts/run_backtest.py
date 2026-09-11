#!/usr/bin/env python3
"""Rolling-origin backtest. Pure pandas — no Spark, no Databricks imports."""
from __future__ import annotations

import sys

import pandas as pd

sys.path.insert(0, ".")
from src.features.spec import FEATURES_MODEL_A, FEATURES_MODEL_B  # noqa: E402
from src.ml import evaluate as ev  # noqa: E402
from src.ml.backtest import monthly_folds, run_backtest, scoreable  # noqa: E402
from src.ml.models import XGBModel, eia_benchmark, seasonal_naive  # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else "data/features/demand_features"
df = pd.read_parquet(SRC)
print(f"features {len(df):,} rows x {len(df.columns)} cols")

names_a = [s.name for s in FEATURES_MODEL_A]
names_b = [s.name for s in FEATURES_MODEL_B]
# The model trains on exactly the columns the leakage audit checked. Reading the list
# from the registry rather than restating it here is what keeps that true.
missing = [c for c in names_b if c not in df.columns]
assert not missing, f"declared but absent from the table: {missing}"

models = [
    seasonal_naive(),
    eia_benchmark(),
    XGBModel(name="xgb_a", features=names_a),
    XGBModel(name="xgb_b", features=names_b),
]
folds = monthly_folds(df["forecast_date"], min_train_months=12)
print(f"{len(folds)} folds · {folds[0].label} -> {folds[-1].label} · "
      f"{len(models)} models · A={len(names_a)} feats, B={len(names_b)} feats\n")

preds = run_backtest(df, models, folds)
s = scoreable(preds)
print(f"\npredictions {len(preds):,} · scoreable {len(s):,} "
      f"({len(s)//len(models):,} hours x {len(models)} models)")
print(f"dropped {len(preds)-len(s):,}: label missing, or benchmark never answered\n")
preds.to_parquet("data/features/backtest_predictions.parquet", index=False)

pd.set_option("display.width", 160)
def head(t): print("\n" + "=" * 80 + f"\n{t}\n" + "=" * 80)

head("OVERALL — hourly, all scoreable rows")
print(ev.versus(s).to_string(index=False))
head("PEAK — per target day")
print(ev.peak_summary(ev.peak_metrics(s)).to_string(index=False))
head("BY REGIME — extreme = top 5% of training labels, per fold")
print(ev.by_regime(s).to_string(index=False))
head("BY DAY TYPE")
print(ev.by_day_type(s).to_string(index=False))

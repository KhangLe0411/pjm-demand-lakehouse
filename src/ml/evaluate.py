"""Scoring: overall, sliced, and peak-level.

A single headline number hides where a model actually fails, so README section 22 asks
for slices by horizon, hour of day, day type and demand regime. Peak magnitude and
timing are reported separately because they are what an operator plans against, and a
model can be good on average while missing every peak.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.ml.models import LABEL

MODEL_ORDER = ["seasonal_naive", "eia_df", "xgb_a", "xgb_b"]


def _agg(g: pd.DataFrame) -> pd.Series:
    err = g["prediction_mwh"] - g[LABEL]
    return pd.Series({
        "n": len(g),
        "MAE_mwh": err.abs().mean(),
        "RMSE_mwh": np.sqrt((err ** 2).mean()),
        "MAPE_pct": 100 * (err.abs() / g[LABEL].abs()).mean(),
        # Signed, so a model that is consistently low is distinguishable from one that
        # is merely noisy. abs() alone cannot tell those apart.
        "bias_mwh": err.mean(),
    })


def _order(df: pd.DataFrame) -> pd.DataFrame:
    if "model" in df.columns:
        key = pd.Categorical(df["model"], categories=MODEL_ORDER + [
            m for m in df["model"].unique() if m not in MODEL_ORDER], ordered=True)
        df = df.assign(_k=key).sort_values(["_k", *[c for c in df.columns
                                                    if c not in ("model", "_k")][:1]])
        df = df.drop(columns="_k")
    return df


def overall(preds: pd.DataFrame) -> pd.DataFrame:
    return _order(preds.groupby("model", observed=True)
                  .apply(_agg, include_groups=False).reset_index()).round(3)


def by(preds: pd.DataFrame, dim: str) -> pd.DataFrame:
    return (preds.groupby(["model", dim], observed=True)
            .apply(_agg, include_groups=False).reset_index().round(3))


def by_day_type(preds: pd.DataFrame) -> pd.DataFrame:
    d = preds.assign(day_type=np.where(
        preds["is_holiday"], "holiday",
        np.where(preds["is_weekend"], "weekend", "weekday")))
    return by(d, "day_type")


def by_regime(preds: pd.DataFrame) -> pd.DataFrame:
    d = preds.assign(regime=np.where(preds["is_extreme"], "extreme", "normal"))
    return by(d, "regime")


def peak_metrics(preds: pd.DataFrame, require_full_day: bool = True) -> pd.DataFrame:
    """Per model per target day: does it find the right peak, at the right hour?

    Days are dropped unless every hour of the local operating day is present. A day
    holding half its hours would report the maximum of that half as "the peak", which is
    not the same quantity and would flatter whichever model happened to cover the
    afternoon.
    """
    rows = []
    for (model, day), g in preds.groupby(["model", "target_local_date"], observed=True):
        g = g.dropna(subset=[LABEL, "prediction_mwh"])
        if g.empty:
            continue
        if require_full_day:
            expected = g["target_local_hour"].nunique()
            if len(g) != expected or expected < 23:
                continue
        a = g.loc[g[LABEL].idxmax()]
        p = g.loc[g["prediction_mwh"].idxmax()]
        rows.append({
            "model": model, "target_local_date": day,
            "actual_peak_mwh": a[LABEL], "actual_peak_hour": a["target_local_hour"],
            "pred_peak_mwh": p["prediction_mwh"], "pred_peak_hour": p["target_local_hour"],
            "peak_magnitude_error_mwh": p["prediction_mwh"] - a[LABEL],
            "peak_magnitude_error_pct":
                100 * (p["prediction_mwh"] - a[LABEL]) / a[LABEL],
            "peak_timing_error_hours": int(p["target_local_hour"] - a["target_local_hour"]),
            "is_extreme_day": bool(g["is_extreme"].any()),
        })
    return pd.DataFrame(rows)


def peak_summary(pk: pd.DataFrame) -> pd.DataFrame:
    if pk.empty:
        return pk
    out = (pk.groupby("model", observed=True).apply(lambda g: pd.Series({
        "days": len(g),
        "pk_bias_pct": g["peak_magnitude_error_pct"].mean(),
        "pk_MAPE_pct": g["peak_magnitude_error_pct"].abs().mean(),
        "pk_timing_MAE_h": g["peak_timing_error_hours"].abs().mean(),
        "pk_timing_hit_pct": 100 * (g["peak_timing_error_hours"] == 0).mean(),
        # Within an hour either way is operationally close enough to matter as a
        # separate figure from an exact hit.
        "pk_timing_within_1h_pct":
            100 * (g["peak_timing_error_hours"].abs() <= 1).mean(),
    }), include_groups=False).reset_index())
    return _order(out).round(3)


def versus(preds: pd.DataFrame, baseline: str = "eia_df") -> pd.DataFrame:
    """Improvement over a baseline, on the rows both actually scored."""
    o = overall(preds).set_index("model")
    if baseline not in o.index:
        return o.reset_index()
    b = o.loc[baseline]
    out = o.assign(
        MAE_vs_baseline_pct=100 * (o["MAE_mwh"] - b["MAE_mwh"]) / b["MAE_mwh"],
        MAPE_vs_baseline_pp=o["MAPE_pct"] - b["MAPE_pct"],
    )
    return _order(out.reset_index()).round(3)

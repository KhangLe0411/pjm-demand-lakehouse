"""gold.forecast_run / forecast_prediction / forecast_accuracy.

The three-table split from README section 23 exists so that a prediction can always be
traced to the exact fit that produced it. Flattening them into one wide table would
lose that: you could see what was predicted but not what the model knew when it
predicted, and "why was this day bad" becomes unanswerable.

    forecast_run          one forecasting event  - what the model knew
    forecast_prediction   one target hour        - what it said
    forecast_accuracy     one scored hour        - how it did, once actuals arrived

Pandas, matching where backtest output lives. The production daily variant reads
actuals from Silver in Spark, but the scoring logic is the same shape and deliberately
takes plain frames so it can be shared.
"""
from __future__ import annotations

import pandas as pd

from src.ml.models import LABEL

MODEL_VERSION = "v1.0.0"


def _run_id(region: str, model: str, cutoff: pd.Timestamp, version: str) -> str:
    """Readable rather than hashed. A run id that can be eyeballed against a log line
    is worth more here than one that is shorter."""
    return f"{region}__{model}__{version}__{cutoff:%Y%m%dT%H%MZ}"


def build_forecast_run(preds: pd.DataFrame, region_id: str = "PJM",
                       model_version: str = MODEL_VERSION,
                       weather_lead_days: int = 2) -> pd.DataFrame:
    g = (preds.groupby(["model", "cutoff_utc"], observed=True)
         .agg(training_data_end=("train_end", "first"),
              training_rows=("train_rows", "first"),
              fold=("fold", "first"),
              n_targets=("target_timestamp_utc", "nunique"))
         .reset_index())
    g["region_id"] = region_id
    g["model_version"] = model_version
    # Not a run timestamp: the archive gives "N days earlier", and inventing an instant
    # would make the leakage audit assert against something never measured (D-21).
    g["weather_lead_days"] = weather_lead_days
    g["forecast_run_id"] = [
        _run_id(region_id, m, c, model_version)
        for m, c in zip(g["model"], g["cutoff_utc"], strict=False)]
    g["created_at"] = pd.Timestamp.utcnow().tz_localize(None)
    return g[["forecast_run_id", "region_id", "cutoff_utc", "model", "model_version",
              "weather_lead_days", "training_data_end", "training_rows", "fold",
              "n_targets", "created_at"]]


def build_forecast_prediction(preds: pd.DataFrame, runs: pd.DataFrame) -> pd.DataFrame:
    key = runs.set_index(["model", "cutoff_utc"])["forecast_run_id"]
    out = preds.merge(key, left_on=["model", "cutoff_utc"], right_index=True, how="left")
    return out[["forecast_run_id", "target_timestamp_utc", "target_local_date",
                "target_local_hour", "hours_ahead", "prediction_mwh"]].rename(
        columns={"prediction_mwh": "predicted_p50_mwh"})


def build_forecast_accuracy(preds: pd.DataFrame, runs: pd.DataFrame,
                            region_id: str = "PJM") -> pd.DataFrame:
    """Scored only where an actual exists. A row with no actual is not an error of
    zero, and averaging it in as one would flatter every model equally."""
    key = runs.set_index(["model", "cutoff_utc"])["forecast_run_id"]
    d = preds.merge(key, left_on=["model", "cutoff_utc"], right_index=True, how="left")
    d = d[d[LABEL].notna() & d["prediction_mwh"].notna()].copy()

    # is_peak marks the hour that actually held the day's maximum, per model's own
    # target set, so peak accuracy can be filtered without recomputing argmax later.
    idx = d.groupby(["model", "target_local_date"], observed=True)[LABEL].idxmax()
    d["is_peak"] = d.index.isin(idx)

    d["region_id"] = region_id
    d["error_mwh"] = d["prediction_mwh"] - d[LABEL]
    d["absolute_error_mwh"] = d["error_mwh"].abs()
    d["ape"] = d["absolute_error_mwh"] / d[LABEL].abs()
    d["hour_of_day"] = d["target_local_hour"]
    return d[["forecast_run_id", "target_timestamp_utc", "region_id",
              "model", "hours_ahead", "hour_of_day",
              "prediction_mwh", LABEL, "error_mwh", "absolute_error_mwh", "ape",
              "is_extreme", "is_peak", "is_weekend", "is_holiday",
              "target_local_date"]].rename(
        columns={"prediction_mwh": "forecast_mwh", LABEL: "actual_mwh"})


def build_all(preds: pd.DataFrame, **kw) -> dict[str, pd.DataFrame]:
    runs = build_forecast_run(preds, **kw)
    return {
        "forecast_run": runs,
        "forecast_prediction": build_forecast_prediction(preds, runs),
        "forecast_accuracy": build_forecast_accuracy(preds, runs,
                                                     kw.get("region_id", "PJM")),
    }

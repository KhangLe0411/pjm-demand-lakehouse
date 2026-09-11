"""Rolling-origin backtesting with a monthly refit.

Chronological throughout; nothing is shuffled. Each fold trains only on forecast dates
strictly earlier than the month it predicts, so a fold can never see its own future.

Refit cadence is monthly rather than daily (D-06). Refitting per forecast day would be
~1,990 fits to answer the same question ~54 fits answer, and a model retrained nightly
is not what a team would actually operate.

Pure pandas: no Spark, no Databricks. The caller supplies a DataFrame.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from src.ml.models import LABEL, Model

EXTREME_QUANTILE = 0.95


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: date
    train_end: date      # inclusive, last forecast date used for training
    test_start: date
    test_end: date       # inclusive

    @property
    def label(self) -> str:
        return f"{self.test_start:%Y-%m}"


def monthly_folds(forecast_dates: pd.Series, min_train_months: int = 12,
                  window_months: int | None = None) -> list[Fold]:
    """One fold per calendar month, after a warm-up of `min_train_months`.

    The warm-up matters: a model fitted on two weeks would drag the early folds down and
    say more about the warm-up than about the model.

    `window_months` switches from an expanding to a sliding window. Expanding keeps every
    observation, which is right when the process is stationary and wrong when it is not:
    PJM demand rose ~11.5% across this period, so old rows actively mislead about the
    current level. Sliding trades data volume for recency; which wins is measured, not
    assumed.
    """
    d = pd.to_datetime(pd.Series(forecast_dates).drop_duplicates()).sort_values()
    if d.empty:
        return []
    first, last = d.iloc[0], d.iloc[-1]
    months = pd.period_range(first.to_period("M"), last.to_period("M"), freq="M")
    folds, idx = [], 0
    for m in months[min_train_months:]:
        test_start = max(m.start_time.date(), first.date())
        test_end = min(m.end_time.date(), last.date())
        train_end = (m.start_time - pd.Timedelta(days=1)).date()
        train_start = first.date()
        if window_months is not None:
            slide = (m.start_time - pd.DateOffset(months=window_months)).date()
            train_start = max(train_start, slide)
        if test_start > test_end or train_end < train_start:
            continue
        folds.append(Fold(idx, train_start, train_end, test_start, test_end))
        idx += 1
    return folds


def _slice(df: pd.DataFrame, lo: date, hi: date) -> pd.DataFrame:
    fd = df["forecast_date"]
    return df[(fd >= pd.Timestamp(lo)) & (fd <= pd.Timestamp(hi))]


# `cutoff_utc` is carried because a forecast run is identified by its cutoff, not by
# its date: 10:00 local is 14:00 UTC in EDT and 15:00 in EST. Columns absent from the
# input are skipped rather than raising, so a minimal test frame need not supply all.
CARRY = ["forecast_date", "cutoff_utc", "target_timestamp_utc", "target_local_date",
         "target_local_hour", "hours_ahead", "is_weekend", "is_holiday",
         "days_to_nearest_holiday", LABEL, "label_is_suspect"]


def run_backtest(features: pd.DataFrame, models: list[Model],
                 folds: list[Fold], verbose: bool = True) -> pd.DataFrame:
    """Long-format predictions: one row per (fold, model, target hour).

    Every model is scored on the *same* test rows in every fold, including the ones that
    need no fitting. Comparing models across different row sets is the easiest way to
    manufacture a winner.
    """
    df = features.copy()
    df["forecast_date"] = pd.to_datetime(df["forecast_date"])
    out: list[pd.DataFrame] = []

    for f in folds:
        train = _slice(df, f.train_start, f.train_end)
        test = _slice(df, f.test_start, f.test_end)
        if test.empty or train[LABEL].notna().sum() == 0:
            continue

        # Threshold for the extreme regime comes from TRAINING labels only. Deriving it
        # from the full series would leak the test period's own extremes into the
        # definition of what counts as extreme (README section 19).
        thr = train[LABEL].quantile(EXTREME_QUANTILE)

        base = test[[c for c in CARRY if c in test.columns]].copy()
        base["fold"] = f.index
        base["fold_label"] = f.label
        base["train_end"] = pd.Timestamp(f.train_end)
        base["train_rows"] = int(train[LABEL].notna().sum())
        base["extreme_threshold_mwh"] = thr
        base["is_extreme"] = test[LABEL] >= thr

        for m in models:
            t0 = time.perf_counter()
            if m.needs_fit:
                m.fit(train)
            pred = m.predict(test)
            rec = base.copy()
            rec["model"] = m.name
            rec["prediction_mwh"] = pred
            rec["fit_seconds"] = round(time.perf_counter() - t0, 3)
            out.append(rec)

        if verbose:
            print(f"  fold {f.index:>2} {f.label}  train<= {f.train_end} "
                  f"({int(train[LABEL].notna().sum()):>6,} rows)  test {len(test):>4}")

    if not out:
        return pd.DataFrame(columns=[*CARRY, "model", "prediction_mwh"])
    res = pd.concat(out, ignore_index=True)
    res["error_mwh"] = res["prediction_mwh"] - res[LABEL]
    res["abs_error_mwh"] = res["error_mwh"].abs()
    res["ape"] = np.where(res[LABEL].abs() > 0,
                          res["abs_error_mwh"] / res[LABEL].abs(), np.nan)
    return res


def scoreable(predictions: pd.DataFrame, require_benchmark: bool = True) -> pd.DataFrame:
    """Rows every model can be scored on.

    Both filters exist to keep the comparison honest. A NULL label cannot be scored at
    all. And where the benchmark itself is absent — ~30 hours a year — including the row
    would score the challenger on hours the benchmark never got the chance to answer
    (D-02 follow-on).
    """
    ok = predictions[predictions[LABEL].notna() & predictions["prediction_mwh"].notna()]
    if not require_benchmark:
        return ok
    have = (ok[ok["model"] == "eia_df"]
            .set_index(["forecast_date", "target_timestamp_utc"]).index)
    idx = ok.set_index(["forecast_date", "target_timestamp_utc"]).index
    return ok[idx.isin(have)]

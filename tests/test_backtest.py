"""Backtest harness contracts. The recurring risk here is a harness that quietly
manufactures a winner, so most of these check fairness rather than correctness."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.ml import evaluate as ev
from src.ml.backtest import monthly_folds, run_backtest, scoreable
from src.ml.models import LABEL, ColumnModel, eia_benchmark, seasonal_naive


def features(start="2021-01-01", end="2023-12-31"):
    days = pd.date_range(start, end, freq="D")
    rows = []
    for d in days:
        for h in range(24):
            lab = 100_000 + 20_000 * np.sin(h / 24 * 2 * np.pi) + 500 * (d.month - 6)
            rows.append({
                "forecast_date": d,
                "cutoff_utc": d + pd.Timedelta(hours=14),
                "target_timestamp_utc": d + pd.Timedelta(days=1, hours=h),
                "target_local_date": (d + pd.Timedelta(days=1)).date(),
                "target_local_hour": h,
                "hours_ahead": 14.0 + h,
                "is_weekend": d.weekday() >= 5,
                "is_holiday": False,
                "days_to_nearest_holiday": 5,
                "demand_same_hour_minus_7d": lab * 0.98,
                "benchmark_eia_df_mwh": lab * 1.01,
                "f1": lab * 0.99, "f2": float(h),
                LABEL: lab,
                "label_is_suspect": False,
            })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------- folds

def test_no_fold_can_see_its_own_future():
    f = monthly_folds(features()["forecast_date"], min_train_months=12)
    assert f
    for x in f:
        assert x.train_end < x.test_start, f"fold {x.index} trains into its test window"


def test_folds_are_chronological_and_expanding():
    f = monthly_folds(features()["forecast_date"], min_train_months=12)
    for a, b in zip(f, f[1:], strict=False):
        assert a.test_end < b.test_start          # no overlap, in order
        assert a.train_end < b.train_end          # training window grows
        assert a.train_start == b.train_start     # expanding, not sliding


def test_warm_up_is_respected():
    f = monthly_folds(features()["forecast_date"], min_train_months=12)
    assert f[0].test_start >= date(2022, 1, 1)
    assert len(monthly_folds(features()["forecast_date"], min_train_months=24)) < len(f)


# -------------------------------------------------------------------- fairness

def test_every_model_is_scored_on_identical_rows():
    df = features("2021-01-01", "2022-06-30")
    folds = monthly_folds(df["forecast_date"], 12)
    p = run_backtest(df, [seasonal_naive(), eia_benchmark()], folds, verbose=False)
    keys = {m: set(map(tuple, g[["forecast_date", "target_timestamp_utc"]].values))
            for m, g in p.groupby("model")}
    assert len(set(map(frozenset, keys.values()))) == 1


def test_extreme_threshold_comes_from_training_only():
    """Deriving it from the whole series would let the test period define what counts
    as extreme within it."""
    df = features("2021-01-01", "2022-06-30")
    folds = monthly_folds(df["forecast_date"], 12)
    p = run_backtest(df, [seasonal_naive()], folds, verbose=False)
    for _fold, g in p.groupby("fold"):
        thr = g["extreme_threshold_mwh"].iloc[0]
        train = df[df["forecast_date"] <= g["train_end"].iloc[0]]
        assert thr == pytest.approx(train[LABEL].quantile(0.95))


def test_scoreable_drops_hours_the_benchmark_never_answered():
    df = features("2021-01-01", "2022-03-31")
    df.loc[df.index[:50], "benchmark_eia_df_mwh"] = np.nan
    folds = monthly_folds(df["forecast_date"], 12)
    p = run_backtest(df, [seasonal_naive(), eia_benchmark()], folds, verbose=False)
    s = scoreable(p)
    per_model = s.groupby("model").size()
    assert per_model.nunique() == 1, "models left with different row counts"


# ----------------------------------------------------------- harness sanity

def test_a_model_that_reads_the_label_scores_perfectly():
    """If a cheating model did not score ~0 error, the harness would be measuring
    something other than what it claims."""
    df = features("2021-01-01", "2022-03-31")
    folds = monthly_folds(df["forecast_date"], 12)
    p = run_backtest(df, [ColumnModel("oracle", LABEL)], folds, verbose=False)
    assert p["abs_error_mwh"].max() == pytest.approx(0.0, abs=1e-9)


def test_metrics_are_computed_per_model_not_pooled():
    df = features("2021-01-01", "2022-03-31")
    folds = monthly_folds(df["forecast_date"], 12)
    p = run_backtest(df, [seasonal_naive(), eia_benchmark()], folds, verbose=False)
    o = ev.overall(p)
    assert set(o["model"]) == {"seasonal_naive", "eia_df"}
    assert o["MAE_mwh"].nunique() == 2


# -------------------------------------------------------------------- peaks

def test_partial_days_are_excluded_from_peak_scoring():
    df = features("2021-01-01", "2022-03-31")
    folds = monthly_folds(df["forecast_date"], 12)
    p = run_backtest(df, [seasonal_naive()], folds, verbose=False)
    victim = p["target_local_date"].iloc[0]
    trimmed = p[~((p["target_local_date"] == victim) & (p["target_local_hour"] > 5))]
    pk = ev.peak_metrics(trimmed)
    assert victim not in set(pk["target_local_date"])


def test_peak_timing_error_is_signed_and_hit_rate_matches():
    df = features("2021-01-01", "2022-03-31")
    folds = monthly_folds(df["forecast_date"], 12)
    p = run_backtest(df, [ColumnModel("oracle", LABEL)], folds, verbose=False)
    pk = ev.peak_metrics(p)
    assert (pk["peak_timing_error_hours"] == 0).all()
    assert ev.peak_summary(pk)["pk_timing_hit_pct"].iloc[0] == pytest.approx(100.0)


def test_sliding_window_bounds_training_age():
    """Expanding keeps every row, which is wrong when the level is drifting. Sliding
    caps how old the oldest training row may be."""
    dates = features()["forecast_date"]
    exp = monthly_folds(dates, min_train_months=12)
    sli = monthly_folds(dates, min_train_months=12, window_months=12)
    assert len(exp) == len(sli)
    for a, b in zip(exp, sli, strict=False):
        assert a.test_start == b.test_start and a.test_end == b.test_end  # same test rows
        assert b.train_start >= a.train_start
        assert (b.train_end - b.train_start).days <= 372                  # ~12 months
    assert sli[-1].train_start > sli[0].train_start                       # it slides


def test_ratio_model_is_invariant_to_a_level_shift():
    """The point of the ratio target: multiply the whole series by 1.15 and the model
    should follow, where an absolute-target model would under-predict."""
    from src.ml.models import XGBRatioModel

    df = features("2021-01-01", "2022-06-30")
    df["demand_rolling_mean_24h_to_cutoff"] = df[LABEL] * 0.98
    folds = monthly_folds(df["forecast_date"], 12)
    feats = ["f1", "f2", "target_local_hour"]

    shifted = df.copy()
    for c in (LABEL, "demand_rolling_mean_24h_to_cutoff", "f1"):
        shifted[c] = shifted[c] * 1.15

    m = XGBRatioModel(name="r", features=feats, num_boost_round=40)
    base = run_backtest(df, [m], folds, verbose=False)
    up = run_backtest(shifted, [XGBRatioModel(name="r", features=feats,
                                              num_boost_round=40)], folds, verbose=False)
    # relative error should barely move once the level is scaled out
    assert abs(base["ape"].mean() - up["ape"].mean()) < 0.01

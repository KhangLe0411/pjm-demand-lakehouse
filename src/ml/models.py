"""Forecast models behind one interface.

Deliberately free of Spark and of anything Databricks-specific: every module under
`src/ml/` takes a pandas DataFrame and returns one. Where the frame came from — a local
Parquet file, a Unity Catalog table, a volume on another workspace — is the caller's
concern. That is what lets the identical code run locally, on serverless notebook
compute, and on a prod job cluster without a rewrite.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

LABEL = "label_demand_mwh"
BENCHMARK = "benchmark_eia_df_mwh"


class Model:
    """fit() then predict(). Models needing no fitting simply ignore it."""

    # Plain assignments, not annotations: @dataclass collects annotated attributes from
    # base classes too, and an annotated default here would sort ahead of the
    # subclass's required fields and break its generated __init__.
    name = "base"
    needs_fit = False

    def fit(self, train: pd.DataFrame) -> Model:
        return self

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        raise NotImplementedError


class ColumnModel(Model):
    """A prediction that already exists as a column.

    Both baselines are of this shape, and that is the point: the seasonal naive
    forecast *is* `demand_same_hour_minus_7d`, and the benchmark *is* the aligned EIA
    figure. Re-deriving either here would risk them drifting from the audited feature
    definitions.
    """

    def __init__(self, name: str, column: str):
        self.name, self.column = name, column

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        return test[self.column].to_numpy(dtype=float)


def seasonal_naive() -> ColumnModel:
    """Same clock hour, one week earlier.

    Lag-168 rather than lag-24: under a 10:00 cutoff the previous day's later hours are
    not yet published, so a lag-24 baseline would be quietly using data it could not
    have had (D-04). Lag-168 also preserves day-of-week, which lag-24 does not.
    """
    return ColumnModel("seasonal_naive", "demand_same_hour_minus_7d")


def eia_benchmark() -> ColumnModel:
    """The balancing authority's own published day-ahead forecast, hour-aligned (D-28)."""
    return ColumnModel("eia_df", BENCHMARK)


# kw_only because @dataclass resolves defaults with getattr(), which finds the base
# class's `name` and would then place a defaulted field ahead of the required ones.
# Keyword-only sidesteps field ordering entirely and reads better at the call site.
@dataclass(kw_only=True)
class XGBModel(Model):
    """Gradient-boosted trees over a declared feature list.

    `features` comes from `src.features.spec`, so the columns trained on are exactly the
    columns the leakage audit checked. Passing an arbitrary list would decouple the two
    and make the audit decorative.
    """

    name: str
    features: list[str]
    params: dict = field(default_factory=dict)
    num_boost_round: int = 400
    needs_fit: bool = True
    _booster: object | None = None

    DEFAULTS = {
        "objective": "reg:squarederror",
        "eta": 0.05,
        "max_depth": 6,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 5,
        "tree_method": "hist",   # single-node CPU; the data is ~40k rows
        "seed": 20260909,        # fixed: a backtest that moves between runs cannot be
                                 # compared against a previous one
    }

    def fit(self, train: pd.DataFrame) -> XGBModel:
        import xgboost as xgb

        usable = train.dropna(subset=[LABEL])
        if usable.empty:
            raise ValueError(f"{self.name}: no labelled rows to train on")
        dtrain = xgb.DMatrix(usable[self.features], label=usable[LABEL],
                             feature_names=self.features, missing=np.nan)
        self._booster = xgb.train({**self.DEFAULTS, **self.params}, dtrain,
                                  num_boost_round=self.num_boost_round)
        return self

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        import xgboost as xgb

        if self._booster is None:
            raise RuntimeError(f"{self.name}: predict() before fit()")
        d = xgb.DMatrix(test[self.features], feature_names=self.features, missing=np.nan)
        return np.asarray(self._booster.predict(d), dtype=float)

    def importance(self, kind: str = "gain") -> pd.Series:
        if self._booster is None:
            return pd.Series(dtype=float)
        s = pd.Series(self._booster.get_score(importance_type=kind), dtype=float)
        return s.reindex(self.features).fillna(0.0).sort_values(ascending=False)


@dataclass(kw_only=True)
class XGBRatioModel(XGBModel):
    """Predicts demand as a RATIO to a recent level, then multiplies back.

    Diagnosed cause, not a tuning knob: PJM demand grew ~11.5% between 2022 and 2026
    (data-centre load), so a model fitted on absolute MW over an expanding window
    predicts 2026 at roughly 2023 levels. Measured bias worsened monotonically,
    -497 MWh in 2022 to -1,995 in 2026, and correlated -0.585 with training-set size.

    Modelling `demand / baseline` makes the target scale-free, so a rising level shifts
    the baseline rather than biasing the model. The baseline must itself be a feature
    that passes the leakage audit — `demand_rolling_mean_24h_to_cutoff` ends at
    cutoff-3h and does.
    """

    baseline_col: str = "demand_rolling_mean_24h_to_cutoff"

    def fit(self, train: pd.DataFrame) -> XGBRatioModel:
        import xgboost as xgb

        usable = train.dropna(subset=[LABEL, self.baseline_col])
        usable = usable[usable[self.baseline_col] > 0]
        if usable.empty:
            raise ValueError(f"{self.name}: nothing to train on")
        y = usable[LABEL] / usable[self.baseline_col]
        dtrain = xgb.DMatrix(usable[self.features], label=y,
                             feature_names=self.features, missing=np.nan)
        self._booster = xgb.train({**self.DEFAULTS, **self.params}, dtrain,
                                  num_boost_round=self.num_boost_round)
        return self

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        ratio = super().predict(test)
        base = test[self.baseline_col].to_numpy(dtype=float)
        return ratio * base

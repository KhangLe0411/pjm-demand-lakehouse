"""Registry contracts, exercised against a local MLflow file store.

Same code path the job uses; only the tracking/registry URI differs. That is the point
of keeping `src/ml/registry.py` free of Databricks imports.
"""
import numpy as np
import pandas as pd
import pytest

from src.ml.models import LABEL, XGBModel
from src.ml.registry import (
    champion_metric,
    decide_promotion,
    load_champion,
    log_and_register,
    set_champion,
)

NAME = "demand_forecaster_test"
FEATS = ["f1", "f2", "f3"]


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """SQLite, not the file store: MLflow 3.x refuses the latter, and the model
    registry needs a database backend anyway. The job uses `databricks-uc` — only the
    URI differs, which is what keeps `src/ml/registry.py` free of Databricks imports.
    """
    import mlflow

    d = tmp_path_factory.mktemp("mlruns")
    uri = f"sqlite:///{d}/mlflow.db"
    mlflow.set_tracking_uri(uri)
    mlflow.set_registry_uri(uri)
    return d


def frame(n=400, scale=1.0, seed=0):
    rng = np.random.default_rng(seed)
    f1 = rng.normal(100, 10, n)
    f2 = rng.normal(50, 5, n)
    df = pd.DataFrame({"f1": f1, "f2": f2, "f3": rng.normal(0, 1, n)})
    df[LABEL] = (900 * f1 + 40 * f2 + rng.normal(0, 50, n)) * scale
    return df


def fitted(df, features=FEATS):
    return XGBModel(name="xgb_t", features=features, num_boost_round=30).fit(df)


# ------------------------------------------------------------------ promotion

@pytest.mark.parametrize("cand,champ,expect", [
    (2500.0, None,   True),    # nothing served yet — something must be
    (2500.0, 2600.0, True),    # clearly better
    (2650.0, 2600.0, True),    # inside the tolerance band
    (2700.0, 2600.0, False),   # outside it
])
def test_promotion_requires_evidence(cand, champ, expect):
    assert decide_promotion(cand, champ).promote is expect


def test_tolerance_band_stops_the_alias_random_walking():
    """Without a band, month-to-month noise alone would flip the served model."""
    champ = 2600.0
    noise = [2598.0, 2611.0, 2590.0, 2620.0]
    assert all(decide_promotion(m, champ).promote for m in noise)
    assert not decide_promotion(champ * 1.05, champ).promote


# ------------------------------------------------------------- register / load

def test_registering_does_not_promote(store):
    df = frame()
    info, _ = log_and_register(fitted(df), df, run_name="v1", registered_name=NAME,
                               extra_metrics={"gate_holdout_mae": 500.0})
    assert str(info.registered_model_version) == "1"
    # An alias has to be set deliberately; registering alone must not serve anything.
    assert champion_metric(NAME) is None


def test_champion_metric_reads_the_aliased_version(store):
    set_champion(NAME, "1")
    assert champion_metric(NAME) == pytest.approx(500.0)


def test_round_trip_predicts_the_same_values(store):
    df = frame(seed=1)
    m = fitted(df)
    log_and_register(m, df, run_name="v2", registered_name=NAME,
                     extra_metrics={"gate_holdout_mae": 480.0})
    set_champion(NAME, "2")
    loaded = load_champion(NAME)
    np.testing.assert_allclose(loaded.predict(df), m.predict(df), rtol=1e-6)


# -------------------------------------------------- the wrapper carries a contract

def test_missing_feature_raises_instead_of_predicting(store):
    """A Booster given the wrong columns returns numbers, not an error. The wrapper's
    job is to turn that into a failure."""
    df = frame(seed=2)
    loaded = load_champion(NAME)
    with pytest.raises(Exception, match="missing"):
        loaded.predict(df.drop(columns=["f2"]))


def test_reordered_columns_still_predict_correctly(store):
    """XGBoost binds by position once a DMatrix is built, so a reordered frame would
    predict confidently and wrongly if the wrapper did not reindex."""
    df = frame(seed=3)
    loaded = load_champion(NAME)
    shuffled = df[["f3", LABEL, "f2", "f1"]]
    np.testing.assert_allclose(loaded.predict(shuffled), loaded.predict(df), rtol=1e-9)


def test_extra_columns_are_ignored(store):
    df = frame(seed=4)
    loaded = load_champion(NAME)
    wide = df.assign(unrelated=1.0, another="x")
    np.testing.assert_allclose(loaded.predict(wide), loaded.predict(df), rtol=1e-9)


def test_legacy_metric_name_is_still_read(store):
    """Versions registered before the rename carry `holdout_mae`. The quantity is
    identical — only the name became clearer — so the gate must still find it, or the
    first refit after the rename would promote blindly, which is the one thing the gate
    exists to prevent."""
    df = frame(seed=5)
    log_and_register(fitted(df), df, run_name="legacy", registered_name=NAME,
                     extra_metrics={"holdout_mae": 442.0})
    set_champion(NAME, "3")
    assert champion_metric(NAME) == pytest.approx(442.0)

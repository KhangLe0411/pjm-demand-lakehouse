"""Model packaging and registration.

Two things this exists to prevent.

**Column drift.** An XGBoost Booster carries no memory of which DataFrame columns it
was trained on, or in what order. Handing it a frame whose columns moved produces
numbers, not an error. The wrapper therefore carries the feature list and validates the
input against it, so a mismatch fails loudly at predict time instead of quietly
degrading a forecast nobody re-checks.

**Automatic promotion.** Registering a version and serving it are separate decisions.
Concept drift was measured in this project (D-29): a refit is not guaranteed to be an
improvement, so the alias only moves when the candidate earns it.

Pure Python and MLflow — no Spark, no Databricks imports — so the same code registers
to a local SQLite store in tests and to Unity Catalog in the job. Only the URI differs.
"""
from __future__ import annotations

from dataclasses import dataclass

import mlflow
import numpy as np
import pandas as pd

REGISTERED_NAME = "energy.ml.demand_forecaster"
CHAMPION = "champion"

# Named so it cannot be mistaken for a score of the served artifact. The served model is
# fitted on ALL labelled data; this is the holdout MAE of a *gating* model fitted on
# everything before the holdout. Two different artifacts, deliberately:
#
#   gate model    train-only  -> honest out-of-sample score, used for the decision
#   served model  all data    -> what actually gets predictions, ~9% better MAE here
#
# Registering only the gate model was tried first and measured: it cost +9.1% MAE,
# because with drift the withheld months are the ones that matter (D-29, D-37).
#
# This line used to claim the metric "is compared like-for-like across versions". It was
# not: the incumbent's number came from its own run, measured on the holdout as it stood
# then, and the holdout grows with the data. It is like-for-like now because the
# incumbent's gate is refitted and re-scored on the candidate's holdout at decision time
# — see `champion_training_cutoff` and D-43. The metric name alone never guaranteed it.
GATE_METRIC = "gate_holdout_mae"


class DemandForecaster(mlflow.pyfunc.PythonModel):
    """pyfunc wrapper: the Booster plus the contract it was trained under."""

    def __init__(self, booster=None, features: list[str] | None = None):
        self.booster = booster
        self.features = features or []

    def load_context(self, context):
        import json

        import xgboost as xgb

        self.booster = xgb.Booster()
        self.booster.load_model(context.artifacts["booster"])
        with open(context.artifacts["features"]) as f:
            self.features = json.load(f)

    def predict(self, context, model_input: pd.DataFrame, params=None):
        missing = [c for c in self.features if c not in model_input.columns]
        if missing:
            raise ValueError(
                f"input is missing {len(missing)} of the model's features: "
                f"{missing[:5]}{'...' if len(missing) > 5 else ''}")
        import xgboost as xgb

        # Reindexed to the training order rather than trusted. XGBoost binds by
        # position once a DMatrix is built, so a reordered frame predicts confidently
        # and wrongly.
        d = xgb.DMatrix(model_input[self.features], feature_names=self.features,
                        missing=np.nan)
        return np.asarray(self.booster.predict(d), dtype=float)


@dataclass(frozen=True)
class PromotionVerdict:
    promote: bool
    reason: str
    candidate_mae: float
    champion_mae: float | None

    def __str__(self) -> str:
        c = "n/a" if self.champion_mae is None else f"{self.champion_mae:,.1f}"
        return (f"{'PROMOTE' if self.promote else 'HOLD'} — {self.reason} "
                f"(candidate {self.candidate_mae:,.1f} vs champion {c})")


@dataclass(frozen=True)
class ComparisonBasis:
    informative: bool
    description: str


def describe_comparison(candidate_trained_through: str,
                        incumbent_trained_through: str | None) -> ComparisonBasis:
    """Whether the gate comparison can distinguish the two models at all.

    Refitting the incumbent on its own window and scoring it on the candidate's holdout
    (D-43) makes the two numbers comparable. It does not make them *different*: the
    holdout begins at a month boundary, so `train` is everything before that boundary,
    and two refits in the same month train the identical gate on the identical rows.
    The scores then match to every decimal and the gate reports "within 2%" — which is
    true, and carries no information.

    On the monthly schedule this does not arise: each run falls in a new month and the
    boundary moves. It arises when someone refits by hand mid-month, which is exactly
    when a reassuring message is least deserved. Promotion still proceeds — the served
    model has strictly more labelled data and nothing argues against it — but it is
    recorded as an absence of evidence, not as evidence.
    """
    if incumbent_trained_through is None:
        return ComparisonBasis(False, "no champion yet — promotion is unconditional")
    if incumbent_trained_through == candidate_trained_through:
        return ComparisonBasis(
            False,
            "degenerate: the incumbent gate has the same training window "
            f"(through {candidate_trained_through}), so both gates are the same model")
    return ComparisonBasis(
        True,
        f"incumbent gate refitted through {incumbent_trained_through}, "
        f"candidate through {candidate_trained_through}, scored on the same holdout")


def decide_promotion(candidate_mae: float, champion_mae: float | None,
                     tolerance_pct: float = 2.0) -> PromotionVerdict:
    """Promote only on evidence, with a tolerance band.

    The band matters in both directions. Without it, noise alone flips the alias every
    month and the served model becomes a random walk; with it too wide, a genuinely
    better model never ships. 2% of MAE is roughly the month-to-month spread seen in
    the backtest.

    No champion yet is the one case where promotion is unconditional — something has to
    be served first.
    """
    if champion_mae is None:
        return PromotionVerdict(True, "no champion yet", candidate_mae, None)
    threshold = champion_mae * (1 + tolerance_pct / 100)
    if candidate_mae <= threshold:
        return PromotionVerdict(
            True, f"within {tolerance_pct:.0f}% of champion", candidate_mae, champion_mae)
    return PromotionVerdict(
        False, f"worse than champion by more than {tolerance_pct:.0f}%",
        candidate_mae, champion_mae)


def log_and_register(model, train: pd.DataFrame, *, run_name: str,
                     registered_name: str = REGISTERED_NAME,
                     extra_metrics: dict | None = None,
                     extra_params: dict | None = None):
    """Log the fitted booster as a pyfunc and register a new version.

    Registering is not promoting. The alias is moved separately, and only on evidence
    — see `decide_promotion`.
    """
    import json
    import tempfile
    from pathlib import Path

    import mlflow
    from mlflow.models import infer_signature

    if model._booster is None:
        raise ValueError("model must be fitted before logging")

    with tempfile.TemporaryDirectory() as tmp:
        bpath = Path(tmp) / "booster.json"
        fpath = Path(tmp) / "features.json"
        model._booster.save_model(str(bpath))
        fpath.write_text(json.dumps(model.features))

        # An input example is logged alongside the signature so the stored contract can
        # be validated end to end rather than only described.
        sample = train[model.features].head(5)
        signature = infer_signature(sample, model.predict(train.head(5)))

        with mlflow.start_run(run_name=run_name) as run:
            mlflow.log_params({
                "model": model.name,
                "n_features": len(model.features),
                "num_boost_round": model.num_boost_round,
                "train_rows": int(len(train)),
                # Logged because a backtest that cannot be reproduced is an anecdote.
                "seed": model.DEFAULTS["seed"],
                **(extra_params or {}),
            })
            if extra_metrics:
                mlflow.log_metrics(extra_metrics)
            # The feature list is an artifact *and* a param: the artifact is what the
            # wrapper loads, the param is what a human reads in the UI when asking
            # why two versions disagree.
            mlflow.log_dict({"features": model.features}, "features.json")

            info = mlflow.pyfunc.log_model(
                name="model",
                python_model=DemandForecaster(),
                artifacts={"booster": str(bpath), "features": str(fpath)},
                signature=signature,
                input_example=sample,
                registered_model_name=registered_name,
            )
    return info, run.info.run_id


def champion_training_cutoff(registered_name: str = REGISTERED_NAME,
                             alias: str = CHAMPION) -> str | None:
    """The last `forecast_date` the champion's gate model was trained on.

    Returned so the incumbent can be re-fitted on its own window and scored on the same
    holdout as the candidate. Comparing against its *recorded* score instead was unsound
    for a reason that only showed up once the data grew: the holdout starts at a fixed
    month boundary and ends wherever the data does, so each refit scores on a superset
    of the previous one. 7.5% of the rows differed the first time it mattered — enough
    to account for the entire gap the gate rejected on (D-43).

    `None` means there is no champion, which promotes unconditionally. A champion
    *without* the parameter raises instead: it was registered before this was recorded,
    so an honest comparison is not available, and silently falling back to the recorded
    score would reintroduce exactly the bias this replaces.
    """
    from mlflow.exceptions import MlflowException

    client = mlflow.MlflowClient()
    try:
        mv = client.get_model_version_by_alias(registered_name, alias)
    except (MlflowException, Exception):
        return None
    cutoff = client.get_run(mv.run_id).data.params.get("gate_model_trained_through")
    if cutoff is None:
        raise RuntimeError(
            f"{registered_name}@{alias} (v{mv.version}) has no "
            "`gate_model_trained_through` param, so its gate model cannot be refitted "
            "for a like-for-like comparison. Register a version that records it.")
    return cutoff


def set_champion(registered_name: str, version: str, alias: str = CHAMPION) -> None:
    import mlflow

    mlflow.MlflowClient().set_registered_model_alias(registered_name, alias, version)


def load_champion(registered_name: str = REGISTERED_NAME, alias: str = CHAMPION):
    import mlflow

    return mlflow.pyfunc.load_model(f"models:/{registered_name}@{alias}")

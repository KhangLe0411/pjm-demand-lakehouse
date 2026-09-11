# Databricks notebook source
# MAGIC %md
# MAGIC # Monthly refit and registration
# MAGIC
# MAGIC Separate from the daily pipeline because the cadences differ: fitting is monthly
# MAGIC (D-06), inference is daily and takes seconds. Collapsing them means paying for
# MAGIC 30 refits a month to get one month's worth of learning.
# MAGIC
# MAGIC **Registering is not promoting.** Concept drift was measured in this project —
# MAGIC demand rose 11.5% across the backtest and the model's bias tracked it (D-29) —
# MAGIC so a refit is not automatically an improvement. The alias moves only when the
# MAGIC candidate earns it against the incumbent's own recorded gate score.
# MAGIC
# MAGIC **Two models are fitted, on purpose.**
# MAGIC
# MAGIC | | trained on | role |
# MAGIC |---|---|---|
# MAGIC | gate | everything before the holdout | scored out-of-sample; decides promotion |
# MAGIC | served | all labelled data | registered and actually used |
# MAGIC
# MAGIC Registering only the gate model was tried first and measured: it cost **+9.1%
# MAGIC MAE**, because under drift the withheld months are precisely the ones that
# MAGIC matter. The metric is therefore named `gate_holdout_mae` rather than something
# MAGIC that reads as a score of the served artifact — the comparison stays
# MAGIC like-for-like across versions, and nothing pretends to be what it is not.

# COMMAND ----------

import os
import sys

_p = "/Workspace" + dbutils.notebook.entry_point.getDbutils().notebook() \
    .getContext().notebookPath().get()
for _ in range(5):
    _p = os.path.dirname(_p)
    if os.path.isdir(os.path.join(_p, "src")):
        sys.path.insert(0, _p)
        break
else:
    raise RuntimeError("could not locate src/ from the notebook path")

dbutils.widgets.text("catalog", "energy")
dbutils.widgets.text("holdout_months", "2")
dbutils.widgets.text("tolerance_pct", "2.0")
dbutils.widgets.text("experiment_path", "")
CATALOG = dbutils.widgets.get("catalog")
HOLDOUT = int(dbutils.widgets.get("holdout_months"))
TOL = float(dbutils.widgets.get("tolerance_pct"))
EXPERIMENT = dbutils.widgets.get("experiment_path")
spark.sql(f"USE CATALOG {CATALOG}")

# COMMAND ----------

import mlflow                                                  # noqa: E402
import pandas as pd                                            # noqa: E402
from src.features.spec import FEATURES_MODEL_B                 # noqa: E402
from src.ml.models import LABEL, XGBModel                      # noqa: E402
from src.ml import registry as reg                             # noqa: E402

mlflow.set_registry_uri("databricks-uc")

# Without this, MLflow logs to the notebook's own path — so the experiment, and with it
# every run a registered version points at, lives inside the bundle deployment folder.
# That is how the champion became unreadable after prod moved to the service principal
# (D-40). Empty means "not passed", which should fail here rather than silently fall
# back to the default that caused the problem.
if not EXPERIMENT:
    raise RuntimeError(
        "experiment_path is empty. Pass it from the job so runs land in a fixed "
        "location instead of under whatever path this notebook was deployed to.")
mlflow.set_experiment(EXPERIMENT)
print(f"experiment: {EXPERIMENT}")
NAME = f"{CATALOG}.ml.demand_forecaster"

df = spark.table(f"{CATALOG}.gold.demand_features").toPandas()
df["forecast_date"] = pd.to_datetime(df["forecast_date"])
cut = (df["forecast_date"].max().to_period("M") - (HOLDOUT - 1)).start_time
train, holdout = df[df["forecast_date"] < cut], df[df["forecast_date"] >= cut]
print(f"train {len(train):,} rows (< {cut.date()}) · holdout {len(holdout):,} rows")

# COMMAND ----------

feats = [s.name for s in FEATURES_MODEL_B]

# 1. Gate model — train-only, scored out-of-sample. Never served.
gate = XGBModel(name="xgb_b_gate", features=feats).fit(train)
scored = holdout.dropna(subset=[LABEL])
err = gate.predict(scored) - scored[LABEL]
metrics = {
    reg.GATE_METRIC: float(err.abs().mean()),
    "gate_holdout_mape_pct": float(100 * (err.abs() / scored[LABEL].abs()).mean()),
    # Signed, because drift shows up as bias long before it shows up as MAE (D-29).
    "gate_holdout_bias": float(err.mean()),
    "gate_holdout_rows": float(len(scored)),
}
print("gate:", metrics)

# 2. Served model — all labelled data, including the holdout. This is what gets
# registered. Under drift the most recent months carry the most information, which is
# why withholding them from the served artifact was expensive.
labelled = df.dropna(subset=[LABEL])
served = XGBModel(name="xgb_b", features=feats).fit(labelled)

# The latest LABEL the model saw, not the latest forecast_date. They differ: a run on
# day D carries labels for targets on D+1, so recording the forecast date understates
# the model's knowledge by a day and marks genuinely in-sample hours as out-of-sample —
# the unsafe direction, and the reason this flag took three attempts to get right.
max_label_ts = labelled["target_timestamp_utc"].max()
print(f"served model fitted on {len(labelled):,} labelled rows; "
      f"latest label seen {max_label_ts}")

# COMMAND ----------

# The SERVED model is registered; the GATE model's score rides along as metrics. The
# params below exist so nobody has to reconstruct which artifact the number describes.
info, run_id = reg.log_and_register(
    served, df.dropna(subset=[LABEL]), run_name=f"refit-{cut:%Y%m}",
    registered_name=NAME,
    extra_metrics=metrics,
    extra_params={
        "gate_holdout_from": str(cut.date()),
        "gate_holdout_months": HOLDOUT,
        "gate_model_trained_through": str(train["forecast_date"].max().date()),
        "served_model_max_label_ts": str(max_label_ts),
        "metric_describes": "gate model, not the served artifact",
    },
)
version = str(info.registered_model_version)
print(f"registered {NAME} version {version}")

# COMMAND ----------

incumbent = reg.champion_metric(NAME)
verdict = reg.decide_promotion(metrics[reg.GATE_METRIC], incumbent, tolerance_pct=TOL)
print(verdict)
if verdict.promote:
    reg.set_champion(NAME, version)
    print(f"alias @champion -> version {version}")
else:
    print(f"alias @champion unchanged; version {version} registered but not served")

# COMMAND ----------

import json  # noqa: E402
out = {"model": NAME, "version": version, "promoted": verdict.promote,
       "reason": verdict.reason, **{k: round(v, 3) for k, v in metrics.items()}}
print(json.dumps(out, indent=2))
dbutils.notebook.exit(json.dumps(out))

# Databricks notebook source
# MAGIC %md
# MAGIC # Forecast
# MAGIC
# MAGIC Loads the registered champion rather than fitting. Fitting here would repeat a
# MAGIC monthly job every day and, worse, would quietly serve a different artifact each
# MAGIC run — so a forecast could not be traced back to the model that produced it.
# MAGIC
# MAGIC If no champion is set this task **fails**. A silent fall-back to fitting inline
# MAGIC is the kind of convenience that hides a broken promotion gate: the pipeline
# MAGIC would keep emitting plausible numbers while nothing was actually being served.
# MAGIC Run `energy_monthly_refit` first.

# COMMAND ----------

import os
import sys
import time

# Phase timings, kept after the tuning that produced them. Measured breakdown at 78 s
# task / 37 s notebook showed the gap was dependency installation, not anything the
# notebook does — toPandas was 1.1 s against an assumed-large share. Dependencies now
# come from the job environment; these marks stay so the next regression is visible
# rather than argued about.
_T0 = time.perf_counter()
_MARKS = []


def mark(label):
    _MARKS.append((label, time.perf_counter() - _T0))
    print(f"  [{_MARKS[-1][1]:6.1f}s] {label}")


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
CATALOG = dbutils.widgets.get("catalog")
spark.sql(f"USE CATALOG {CATALOG}")
print(f"catalog={CATALOG}  root={sys.path[0]}")
mark("widgets + sys.path")

# COMMAND ----------

import mlflow                                                   # noqa: E402
import pandas as pd                                             # noqa: E402
from src.gold.forecast_tables import (                          # noqa: E402
    build_forecast_prediction, build_forecast_run)
from src.ml import registry as reg                               # noqa: E402
from src.ml.models import eia_benchmark, seasonal_naive          # noqa: E402

mark("imports")
mlflow.set_registry_uri("databricks-uc")
NAME = f"{CATALOG}.ml.demand_forecaster"

df = spark.table(f"{CATALOG}.gold.demand_features").toPandas()
mark("toPandas")
df["forecast_date"] = pd.to_datetime(df["forecast_date"])
month = df["forecast_date"].max().to_period("M")
test = df[df["forecast_date"] >= month.start_time]
print(f"predicting {len(test):,} rows in {month}")

champion = reg.load_champion(NAME)          # raises if @champion is unset
mark("load_champion")
client = mlflow.MlflowClient()
mv = client.get_model_version_by_alias(NAME, reg.CHAMPION)

# The training cutoff is read from the artifact's own run, not assumed from the
# calendar. Assuming it was the earlier bug: the local guess (month start minus a day)
# was stale once fitting moved to the registry, so the downstream in-sample flag
# compared against the wrong date and confidently reported zero in-sample rows —
# worse than having no flag, because it looked checked.
_params = client.get_run(mv.run_id).data.params
max_label_ts = _params.get("served_model_max_label_ts")
if max_label_ts is None:
    raise RuntimeError(
        f"{NAME} v{mv.version} has no `served_model_max_label_ts` param. Versions "
        "registered before this was recorded cannot support an honest in-sample flag; "
        "run energy_monthly_refit to register one that can.")
print(f"champion: {NAME} v{mv.version} (run {mv.run_id}), "
      f"latest label seen {max_label_ts}")

# COMMAND ----------


class RegisteredModel:
    """Adapts the loaded pyfunc to the interface the baselines already use, so the
    comparison table is assembled the same way regardless of where a prediction came
    from."""

    name = "xgb_b"
    needs_fit = False

    def predict(self, frame):
        return champion.predict(frame)


models = [seasonal_naive(), eia_benchmark(), RegisteredModel()]

out = []
for m in models:
    rec = test[["forecast_date", "cutoff_utc", "target_timestamp_utc",
                "target_local_date", "target_local_hour", "hours_ahead"]].copy()
    rec["model"] = m.name
    rec["prediction_mwh"] = m.predict(test)
    # Baselines fit nothing, so no target is ever in-sample for them. NaT rather than
    # a date makes that explicit instead of encoding it as a very old timestamp.
    rec["train_end"] = (pd.Timestamp(max_label_ts) if m.name == "xgb_b" else pd.NaT)
    # Not fitted here; provenance lives in the registry, keyed by model_version.
    rec["train_rows"] = 0
    rec["fold"] = 0
    rec["model_version"] = mv.version
    out.append(rec)
preds = pd.concat(out, ignore_index=True)
mark("predict")

runs = build_forecast_run(preds)
spark.createDataFrame(runs).write.mode("overwrite").option("overwriteSchema", "true") \
     .saveAsTable(f"{CATALOG}.gold.forecast_run")
spark.createDataFrame(build_forecast_prediction(preds, runs)) \
     .write.mode("overwrite").option("overwriteSchema", "true") \
     .saveAsTable(f"{CATALOG}.gold.forecast_prediction")

# COMMAND ----------

import json  # noqa: E402
mark("write tables")
phases = [{"phase": a, "cumulative_s": round(b, 1)} for a, b in _MARKS]
o = {"month": str(month), "runs": len(runs), "predictions": len(preds),
     "models": sorted(preds["model"].unique().tolist()),
     "champion_version": mv.version,
     "phases": phases,
     "notebook_total_s": round(_MARKS[-1][1], 1)}
print(json.dumps(o, indent=2))
dbutils.notebook.exit(json.dumps(o))

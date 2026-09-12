# Databricks notebook source
import os
import sys

# The tested transforms live in src/. Notebooks call them rather than restating the
# logic: a copy would drift from the version the test suite covers, and the drift
# would not announce itself.
#
# The repo root is found by walking up until `src/` appears, so the same notebook works
# whether it was uploaded by hand (src as a sibling) or synced by a bundle (src beside
# notebooks/, one level further up).
_p = "/Workspace" + dbutils.notebook.entry_point.getDbutils().notebook() \
    .getContext().notebookPath().get()
for _ in range(5):
    _p = os.path.dirname(_p)
    if os.path.isdir(os.path.join(_p, "src")):
        sys.path.insert(0, _p)
        break
else:
    raise RuntimeError("could not locate src/ from the notebook path")

# A parameter, not a constant: the bundle target supplies it, so dev and prod differ in
# configuration rather than in code.
dbutils.widgets.text("catalog", "energy")
CATALOG = dbutils.widgets.get("catalog")
spark.sql(f"USE CATALOG {CATALOG}")
print(f"catalog={CATALOG}  root={sys.path[0]}")

# COMMAND ----------
# MAGIC %md
# MAGIC # Forecast accuracy
# MAGIC
# MAGIC Scores predictions once actuals arrive. Only hours with a usable actual are
# MAGIC scored — a missing actual is not an error of zero, and averaging it in as one
# MAGIC would flatter every model equally.
# MAGIC
# MAGIC **In-sample rows are marked, not silently mixed in.** The served model is fitted
# MAGIC on all labelled data, so any target at or before its training cutoff was seen
# MAGIC during fitting. Scoring those alongside an out-of-sample benchmark produces a
# MAGIC fabricated win — measured here at MAE 1,573 against the benchmark's 2,856, which
# MAGIC would read as beating a professional forecast by 45%. The flag makes that
# MAGIC impossible to report by accident; the honest number is the out-of-sample split.
# MAGIC
# MAGIC In real operation the overlap does not arise: the model is refit monthly and
# MAGIC predicts forward. It appears here only because the history is backfilled.

# COMMAND ----------

from pyspark.sql import functions as F                      # noqa: E402

pred = spark.table(f"{CATALOG}.gold.forecast_prediction")
runs = spark.table(f"{CATALOG}.gold.forecast_run").select(
    "forecast_run_id", "model", "model_version", "training_data_end")
silver = spark.table(f"{CATALOG}.silver.electricity_hourly").select(
    F.col("event_timestamp_utc").alias("target_timestamp_utc"),
    F.col("actual_demand_mwh").alias("actual_mwh"),
    "is_demand_anomaly_suspect", "is_above_historical_record", "local_hour")

acc = (pred.join(runs, "forecast_run_id")
       .join(silver, "target_timestamp_utc")
       # A reading the pipeline already doubts must not be scored against, or the
       # model is graded on data the pipeline itself rejects.
       .filter("actual_mwh IS NOT NULL AND NOT is_demand_anomaly_suspect "
               "AND NOT is_above_historical_record")
       .withColumn("error_mwh", F.col("predicted_p50_mwh") - F.col("actual_mwh"))
       .withColumn("absolute_error_mwh", F.abs("error_mwh"))
       .withColumn("ape", F.col("absolute_error_mwh") / F.abs("actual_mwh"))
       .withColumn("region_id", F.lit("PJM"))
       # A target at or before the training cutoff was seen while fitting. Baselines
       # fit nothing, so they are never in-sample regardless of the date.
       # NULL training_data_end means the model fits nothing, so nothing is in-sample.
       # Everything else compares against the cutoff the artifact itself recorded.
       .withColumn("is_in_sample",
                   F.when(F.col("training_data_end").isNull(), F.lit(False))
                    .otherwise(F.col("target_timestamp_utc")
                               <= F.col("training_data_end").cast("timestamp")))
       .withColumnRenamed("predicted_p50_mwh", "forecast_mwh")
       .withColumnRenamed("local_hour", "hour_of_day"))

# COMMAND ----------

# `is_peak` and `is_extreme` — the two slices the Power BI page is required to show.
# Section 26 forbids reporting peak-timing accuracy without a regime split, and the
# Elliott case study is the reason: on 24-25 Dec 2022 every model missed the peak by
# 9-13 hours, and the 48.4% headline hides it. Without these columns that guardrail
# cannot be built, which is how `docs/powerbi.md` came to specify DAX against two
# columns this table never had.

from pyspark.sql import Window  # noqa: E402

from src.ml.backtest import EXTREME_QUANTILE  # noqa: E402

# The threshold comes from data the serving model had already seen, never from the
# period being scored — otherwise the hours under evaluation help define what counts as
# extreme within themselves (README section 19). The backtest takes it per fold; here
# there is one serving cutoff, so there is one threshold and every model is measured
# against the same one. That makes the prod regime slices comparable across models,
# which the backtest's deliberately are not (D-29).
cutoff = runs.filter("training_data_end IS NOT NULL") \
             .agg(F.max("training_data_end").alias("c")).first()["c"]
if cutoff is None:
    raise RuntimeError(
        "no model in gold.forecast_run records a training_data_end, so there is no "
        "cutoff to derive the extreme threshold from without using the scored period "
        "itself. Run energy_monthly_refit to register a model that records one.")

threshold = (spark.table(f"{CATALOG}.silver.electricity_hourly")
             .filter(F.col("event_timestamp_utc") <= F.lit(cutoff))
             .filter("actual_demand_mwh IS NOT NULL "
                     "AND NOT is_demand_anomaly_suspect "
                     "AND NOT is_above_historical_record")
             .selectExpr(f"percentile(actual_demand_mwh, {EXTREME_QUANTILE}) AS t")
             .first()["t"])
if threshold is None:
    raise RuntimeError(f"no clean Silver demand at or before {cutoff}")
print(f"extreme threshold (P{EXTREME_QUANTILE:.0%} of demand through {cutoff}): "
      f"{threshold:,.0f} MWh")

# Ranked rather than compared to the daily max: two hours can tie on a float, and a
# day with two peaks would double-count in every peak measure downstream.
peak_rank = Window.partitionBy("model", "target_local_date") \
                  .orderBy(F.col("actual_mwh").desc(), F.col("target_timestamp_utc"))
acc = (acc.withColumn("extreme_threshold_mwh", F.lit(threshold))
          .withColumn("is_extreme", F.col("actual_mwh") >= F.lit(threshold))
          .withColumn("is_peak", F.row_number().over(peak_rank) == 1))

acc.write.mode("overwrite").option("overwriteSchema", "true") \
   .saveAsTable(f"{CATALOG}.gold.forecast_accuracy")

# COMMAND ----------

import json  # noqa: E402
a = spark.table(f"{CATALOG}.gold.forecast_accuracy")
def summarise(frame):
    return {r["model"]: {"n": r["n"], "MAE": r["MAE_mwh"], "MAPE": r["MAPE_pct"],
                         "bias": r["bias_mwh"]}
            for r in frame.groupBy("model").agg(
                F.count("*").alias("n"),
                F.round(F.avg("absolute_error_mwh"), 1).alias("MAE_mwh"),
                F.round(100 * F.avg("ape"), 3).alias("MAPE_pct"),
                F.round(F.avg("error_mwh"), 1).alias("bias_mwh")).collect()}


oos = a.filter("NOT is_in_sample")
out = {
    # Reported first and named plainly, because this is the only one that measures
    # anything. An empty split is a real answer: it means this model has not yet
    # predicted a single hour it had not already seen.
    "out_of_sample": summarise(oos),
    "in_sample_excluded_rows": a.filter("is_in_sample").count(),
    "all_rows_unsafe_to_compare": summarise(a),
    # Surfaced because a slice nobody can see the size of is a slice nobody checks.
    # `peak_hours` should equal one per model per day with an actual.
    "extreme_threshold_mwh": round(threshold, 1),
    "peak_hours": a.filter("is_peak").count(),
    "extreme_hours": a.filter("is_extreme").count(),
}
print(json.dumps(out, indent=2))
dbutils.notebook.exit(json.dumps(out))

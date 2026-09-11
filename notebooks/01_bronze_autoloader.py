# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze — Auto Loader
# MAGIC
# MAGIC Landing Parquet on ADLS → `energy.bronze.*` Delta, incrementally.
# MAGIC
# MAGIC This is the one part of the pipeline that cannot run locally: `cloudFiles` is a
# MAGIC Databricks source with no OSS Spark equivalent. Everything downstream — Silver,
# MAGIC Gold, features, models — is engine-portable and developed off-platform at $0
# MAGIC (`docs/decisions.md` D-12).
# MAGIC
# MAGIC **Directory listing, not file notifications.** Enabling notifications would mean
# MAGIC granting the access connector *Storage Account Contributor* — a control-plane role
# MAGIC that also permits reading the account keys — to optimise a listing over ~4,900
# MAGIC files whose cost is cents. Over-privileging an identity for an optimisation the
# MAGIC workload does not need is the worse trade (D-15). The landing layout helps: paths
# MAGIC sort lexicographically by `date=`, so incremental listing never rescans the tree.

# COMMAND ----------

# Parameters, not constants. A bundle target supplies them, so dev and prod differ in
# configuration rather than in code.
#
# `checkpoint_root` is parameterised for the same reason as `catalog`, and it is the
# less obvious one: an Auto Loader checkpoint is per-environment STATE. Sharing it
# between dev and prod would let a dev run advance prod's offsets, after which prod
# skips those files silently - no error, no warning, just missing data.
dbutils.widgets.text("catalog", "energy")
dbutils.widgets.text("checkpoint_root",
                     "abfss://energy@stlakeobs0803.dfs.core.windows.net/_checkpoints")
dbutils.widgets.text("landing_root",
                     "abfss://energy@stlakeobs0803.dfs.core.windows.net/landing")

CATALOG = dbutils.widgets.get("catalog")
LANDING = dbutils.widgets.get("landing_root")
CHECKPOINTS = dbutils.widgets.get("checkpoint_root")
for _k, _v in (("catalog", CATALOG), ("landing", LANDING),
               ("checkpoints", CHECKPOINTS)):
    print(f"{_k:<12} {_v}")

spark.sql(f"USE CATALOG {CATALOG}")
spark.sql("USE SCHEMA bronze")

# COMMAND ----------

from pyspark.sql import functions as F


def ingest(name: str, source_subpath: str, partition_cols: str | None = None):
    """One Auto Loader stream, run to exhaustion and then stopped.

    `availableNow` rather than a continuous trigger: the feed publishes hourly, so a
    24/7 stream would bill continuously to shave minutes off a latency nobody is
    waiting on (D-12). It keeps exactly-once semantics and the checkpoint, and simply
    stops when it runs out of files.

    Append-only, deliberately. A re-fetch of an already-ingested hour arrives as a new
    file with a fresh `ingested_at`, so both vintages land and Silver resolves them.
    Overwriting here would destroy the revision signal the project sets out to measure
    (README section 13).
    """
    src = f"{LANDING}/{source_subpath}"
    ckpt = f"{CHECKPOINTS}/{name}"

    reader = (spark.readStream.format("cloudFiles")
              .option("cloudFiles.format", "parquet")
              .option("cloudFiles.schemaLocation", f"{ckpt}/schema")
              # Explicit rather than defaulted, so the decision is visible in the code
              .option("cloudFiles.useNotifications", "false")
              .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
              .option("cloudFiles.inferColumnTypes", "false"))
    if partition_cols:
        reader = reader.option("cloudFiles.partitionColumns", partition_cols)

    df = (reader.load(src)
          .withColumn("_source_file", F.col("_metadata.file_path"))
          .withColumn("_file_modified", F.col("_metadata.file_modification_time"))
          # Distinct from `ingested_at`, which is when the API was called. This is when
          # Databricks loaded the file; keeping both makes the lineage answerable.
          .withColumn("_bronze_loaded_at", F.current_timestamp()))

    table = f"{CATALOG}.bronze.{name}"
    before = spark.table(table).count() if spark.catalog.tableExists(table) else 0

    q = (df.writeStream.format("delta")
         .outputMode("append")
         .option("checkpointLocation", f"{ckpt}/write")
         .option("mergeSchema", "true")
         .trigger(availableNow=True)
         .toTable(table))
    q.awaitTermination()

    # Measured from the table, not from `lastProgress`. On serverless the progress dict
    # can come back with `numInputRows` unset, and a log line that raises after the
    # write has already committed fails the task for no reason - which is exactly what
    # happened on the first run. The row delta is always available and is the number
    # anyone actually wants.
    after = spark.table(table).count()
    print(f"{name}: {before:,} -> {after:,}  (+{after - before:,} this run)")
    return after - before


# COMMAND ----------

ingest("eia_region_data", "eia/region-data", partition_cols="respondent,date")

# COMMAND ----------

ingest("weather_forecast", "weather", partition_cols="region,date")

# COMMAND ----------

# MAGIC %md ## Verify

# COMMAND ----------

for t in ("eia_region_data", "weather_forecast"):
    n = spark.table(f"{CATALOG}.bronze.{t}").count()
    print(f"{CATALOG}.bronze.{t:<20} {n:>10,} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC Idempotence: re-running this notebook must add **zero** rows, because the
# MAGIC checkpoint already records every file. That is the property that makes an hourly
# MAGIC schedule safe, and it is worth asserting rather than assuming.

# COMMAND ----------

added = ingest("eia_region_data", "eia/region-data", partition_cols="respondent,date")
assert added == 0, f"re-run ingested {added:,} rows again — the checkpoint is not holding"
print("idempotent ✓")

# COMMAND ----------

# MAGIC %md
# MAGIC Exit with a machine-readable summary so a caller — the CLI, a downstream task,
# MAGIC or a monitoring check — can assert on the outcome instead of scraping cell
# MAGIC output. A run that succeeds silently is indistinguishable from one that did
# MAGIC nothing.

# COMMAND ----------

import json

summary = {
    "catalog": CATALOG,
    "tables": {t: spark.table(f"{CATALOG}.bronze.{t}").count()
               for t in ("eia_region_data", "weather_forecast")},
    "distinct_source_files": {
        t: spark.table(f"{CATALOG}.bronze.{t}").select("_source_file").distinct().count()
        for t in ("eia_region_data", "weather_forecast")},
    "vintages": spark.sql(f"""
        SELECT count(*) AS n FROM (
          SELECT period, respondent, type, count(*) AS c
          FROM {CATALOG}.bronze.eia_region_data
          GROUP BY 1,2,3 HAVING c > 1)""").first()["n"],
}
print(json.dumps(summary, indent=2))
dbutils.notebook.exit(json.dumps(summary))

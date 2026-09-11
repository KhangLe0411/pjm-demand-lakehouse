#!/usr/bin/env python3
"""Run Bronze -> Silver over the local landing zone and report what it found.

Local stand-in for the Databricks job: reads the landing Parquet directly instead of a
Bronze Delta table produced by Auto Loader. The transform code is identical.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from pyspark.sql import functions as F

sys.path.insert(0, ".")
from src.silver.calendar_utils import PJM_TZ, expected_hours  # noqa: E402
from src.silver.electricity import build_silver, completeness  # noqa: E402
from src.spark import local_session  # noqa: E402

spark = local_session(app="silver-local")
bronze = spark.read.parquet("data/landing/eia/region-data")
print(f"bronze rows            {bronze.count():>10,}")

silver, quarantine = build_silver(bronze, spark)
silver.cache()
quarantine.cache()
print(f"silver rows            {silver.count():>10,}")
print(f"quarantine rows        {quarantine.count():>10,}")

print("\n=== Quarantine theo reason ===")
quarantine.groupBy("reason").count().orderBy(F.desc("count")).show(truncate=False)

print("=== Quarantine chi tiet ===")
(quarantine.select("period", "metric_type", "raw_value", "reason")
 .orderBy("period").show(20, truncate=False))

print("=== Do phu metric trong Silver ===")
silver.select(
    F.count("*").alias("rows"),
    F.sum(F.col("actual_demand_mwh").isNotNull().cast("int")).alias("D"),
    F.sum(F.col("day_ahead_forecast_mwh").isNotNull().cast("int")).alias("DF"),
    F.sum(F.col("net_generation_mwh").isNotNull().cast("int")).alias("NG"),
    F.sum(F.col("interchange_mwh").isNotNull().cast("int")).alias("TI"),
    F.sum(F.col("is_missing_hour").cast("int")).alias("missing_hour"),
    F.sum(F.col("is_demand_anomaly_suspect").cast("int")).alias("suspect"),
).show()

print("=== Anomaly bi flag (toan bo) ===")
(silver.filter("is_demand_anomaly_suspect")
 .select(F.date_format("event_timestamp_utc", "yyyy-MM-dd'T'HH").alias("utc"),
         "actual_demand_mwh", "local_date", "local_hour")
 .orderBy("utc").show(40, truncate=False))

print("=== DST: ngay van hanh local co 23 / 25 gio ===")
comp = completeness(silver)
exp = spark.createDataFrame(
    [(date(2019, 1, 1) + timedelta(days=i),
      expected_hours(date(2019, 1, 1) + timedelta(days=i), PJM_TZ)) for i in range(2900)],
    ["local_date", "expected_hours"])
joined = comp.join(exp, "local_date")
(joined.filter("expected_hours <> 24")
 .select("local_date", "expected_hours", "actual_hours", "demand_hours")
 .orderBy("local_date").show(20, truncate=False))

print("=== Ngay KHONG khop expected (bug thuc su, neu co) ===")
bad = joined.filter("actual_hours <> expected_hours")
print(f"  so ngay lech: {bad.count():,}")
bad.select("local_date", "expected_hours", "actual_hours") \
   .orderBy("local_date").show(10, truncate=False)

print("=== Revision ===")
silver.select(F.max("revision_count").alias("max_revision"),
              F.sum((F.col("revision_count") > 0).cast("int")).alias("hours_with_revision")).show()
spark.stop()

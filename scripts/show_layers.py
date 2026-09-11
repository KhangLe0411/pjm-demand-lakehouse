#!/usr/bin/env python3
"""Materialise every layer locally and print a sample of each, plus one hour traced
end to end. Bronze is written as a real Delta table rather than described."""
from __future__ import annotations
import sys
from datetime import date
from pyspark.sql import functions as F
sys.path.insert(0, ".")
from src.features.build import build_features                     # noqa: E402
from src.gold.daily_summary import daily_summary                  # noqa: E402
from src.silver.calendar_utils import calendar_frame              # noqa: E402
from src.silver.electricity import build_silver                   # noqa: E402
from src.silver.weather import build_weather_silver               # noqa: E402
from src.spark import local_session                               # noqa: E402

PEAK_UTC = "2026-07-02 22:00:00"
spark = local_session(app="show-layers")


def banner(n, name, note=""):
    print("\n" + "#" * 78)
    print(f"#  LAYER {n} — {name}")
    if note:
        print(f"#  {note}")
    print("#" * 78)


# --------------------------------------------------------------- BRONZE (Delta)
banner(1, "BRONZE", "append-only, raw, value van la STRING; ghi ra Delta that")
landing = (spark.read.parquet("data/landing/eia/region-data")
           .withColumn("_source_file", F.element_at(
               F.split(F.col("_metadata.file_path"), "/"), -1))
           .withColumn("_file_modified", F.col("_metadata.file_modification_time"))
           .withColumn("_bronze_loaded_at", F.current_timestamp()))
landing.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
    .save("data/delta/bronze/eia_region_data")
bronze = spark.read.format("delta").load("data/delta/bronze/eia_region_data")
print(f"\nrows = {bronze.count():,}   schema:")
for f in bronze.schema.fields:
    print(f"    {f.name:<22} {f.dataType.simpleString()}")
print("\n--- 10 dong (thang 8/2026, la khoang co 2 vintage) ---")
(bronze.filter("period LIKE '2026-08-01%'")
 .select("period", "type", "value", "value_units", "ingested_at", "_source_file")
 .orderBy("type", "ingested_at").show(10, truncate=False))

# --------------------------------------------------------------------- SILVER
banner(2, "SILVER", "1 row / gio, da resolve vintage + cast + pivot + DQ + local time")
silver, quarantine = build_silver(bronze, spark)
silver.cache()
print(f"\nrows = {silver.count():,}  (bronze {bronze.count():,} -> long thanh wide)")
print("\n--- 10 dong quanh gio peak ---")
(silver.filter(f"event_timestamp_utc BETWEEN '2026-07-02 18:00:00' AND '2026-07-03 03:00:00'")
 .select(F.date_format("event_timestamp_utc", "MM-dd HH:mm").alias("utc"),
         F.date_format("local_timestamp", "MM-dd HH:mm").alias("local"),
         "local_hour",
         F.col("actual_demand_mwh").alias("demand"),
         F.col("day_ahead_forecast_mwh").alias("eia_df"),
         F.col("net_generation_mwh").alias("net_gen"),
         F.col("interchange_mwh").alias("interch"),
         F.col("is_missing_hour").alias("miss"),
         F.col("is_demand_anomaly_suspect").alias("susp"),
         F.col("is_above_historical_record").alias("above_rec"))
 .orderBy("utc").show(10, truncate=False))

print("--- QUARANTINE: toan bo 7 dong bi loai khoi Silver ---")
quarantine.select("period", "metric_type", "raw_value", "reason").orderBy("period") \
    .show(10, truncate=False)

print("--- SILVER weather: 10 dong, cung gio peak, 8 diem x 2 lead ---")
weather, _ = build_weather_silver(spark.read.parquet("data/landing/weather"))
weather.cache()
(weather.filter(f"valid_timestamp_utc = timestamp'{PEAK_UTC}'")
 .select("point_id", "forecast_lead_days",
         F.round("temperature_c", 1).alias("temp_c"),
         F.round("cdd_c", 2).alias("cdd"), F.round("hdd_c", 2).alias("hdd"),
         F.round("relative_humidity_pct", 0).alias("rh"),
         F.round("wind_speed_kmh", 1).alias("wind"))
 .orderBy("forecast_lead_days", "point_id").show(10, truncate=False))

# ----------------------------------------------------------------------- GOLD
banner(3, "GOLD", "1 row / ngay van hanh local; peak loai row bi flag")
cal = calendar_frame(spark, date(2018, 12, 31), date(2026, 12, 31))
gold = daily_summary(silver, cal).cache()
print(f"\nrows = {gold.count():,}")
print("\n--- 10 ngay quanh peak 2026-07-02 ---")
(gold.filter("local_date BETWEEN date'2026-06-28' AND date'2026-07-07'")
 .select("local_date", "expected_hours", "actual_hours", "demand_hours",
         F.col("peak_demand_mwh").alias("peak"), F.col("peak_local_hour").alias("pk_h"),
         F.col("day_ahead_peak_mwh").alias("df_peak"),
         F.col("day_ahead_peak_local_hour").alias("df_h"),
         F.round("peak_magnitude_error_pct", 2).alias("err_pct"),
         F.col("peak_timing_error_hours").alias("t_err"),
         "load_factor", "is_complete")
 .orderBy("local_date").show(10, truncate=False))

print("--- 10 ngay DST: expected 23/25, khong hard-code 24 ---")
(gold.filter("expected_hours <> 24")
 .select("local_date", "expected_hours", "actual_hours", "demand_hours",
         F.col("peak_demand_mwh").alias("peak"), "is_complete")
 .orderBy(F.desc("local_date")).show(10, truncate=False))

# -------------------------------------------------------------------- FEATURES
banner(4, "GOLD / FEATURES", "1 row / (forecast_date, gio target); 0 leakage")
feats = build_features(silver, weather, spark, date(2026, 6, 28), date(2026, 7, 5)).cache()
print(f"\nrows (8 forecast day) = {feats.count()}   columns = {len(feats.columns)}")
print("\n--- 10 dong: forecast ngay 2026-07-01 -> target ngay 2026-07-02 ---")
(feats.filter("forecast_date = date'2026-07-01'")
 .select(F.date_format("cutoff_utc", "MM-dd HH:mm").alias("cutoff"),
         F.date_format("target_timestamp_utc", "MM-dd HH:mm").alias("target"),
         "target_local_hour", "hours_ahead",
         F.round("demand_cutoff_minus_3h", 0).alias("d_C-3h"),
         F.round("demand_same_hour_minus_2d", 0).alias("d_t-2d"),
         F.round("demand_same_hour_prev_week_mean", 0).alias("d_pw_mean"),
         F.round("temp_c_region_mean_lead2", 1).alias("temp"),
         F.round("cdd_region_lead2", 1).alias("cdd"),
         F.col("label_demand_mwh").alias("LABEL"),
         F.col("benchmark_eia_df_mwh").alias("EIA_DF"))
 .orderBy("target").show(10, truncate=False))
spark.stop()

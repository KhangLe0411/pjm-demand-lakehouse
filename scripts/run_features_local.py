#!/usr/bin/env python3
"""Build the feature table locally, then audit every row of it for leakage."""
from __future__ import annotations

import sys
from datetime import date

from pyspark.sql import functions as F

sys.path.insert(0, ".")
from src.features.build import build_features  # noqa: E402
from src.features.contract import cutoff_utc, target_hours_utc  # noqa: E402
from src.features.spec import FEATURES_MODEL_B, audit  # noqa: E402
from src.silver.electricity import build_silver  # noqa: E402
from src.silver.weather import build_weather_silver  # noqa: E402
from src.spark import local_session  # noqa: E402

START, END = date(2021, 3, 24), date(2026, 9, 6)

spark = local_session(app="features-local")
silver, _ = build_silver(spark.read.parquet("data/landing/eia/region-data"), spark)
weather, wq = build_weather_silver(spark.read.parquet("data/landing/weather"))
silver.cache()
weather.cache()
print(f"silver electricity   {silver.count():>9,}")
print(f"silver weather       {weather.count():>9,}   quarantine {wq.count()}")

feats = build_features(silver, weather, spark, START, END).cache()
n = feats.count()
print(f"feature rows         {n:>9,}   {START} -> {END}")
print(f"feature columns      {len(feats.columns):>9}")

print("\n=== Do phu tung feature (khong null) ===")
declared = [s.name for s in FEATURES_MODEL_B]
cov = feats.select([
    F.round(100 * F.avg(F.col(c).cast("double").isNotNull().cast("int")), 2).alias(c)
    for c in declared]).first().asDict()
for name in declared:
    bar = "#" * int(cov[name] / 4)
    print(f"  {name:<40} {cov[name]:>6.2f}%  {bar}")

print("\n=== Label / benchmark ===")
feats.select(
    F.round(100*F.avg(F.col("label_demand_mwh").isNotNull().cast("int")),2).alias("label_pct"),
    F.round(100*F.avg(F.col("benchmark_eia_df_mwh").isNotNull().cast("int")),2).alias("df_pct"),
    F.sum(F.col("label_is_suspect").cast("int")).alias("label_suspect"),
    F.round(F.min("hours_ahead"),1).alias("h_min"),
    F.round(F.max("hours_ahead"),1).alias("h_max"),
).show(truncate=False)

print("=== LEAKAGE AUDIT tren toan bo cap (cutoff, target) da build ===")
pairs = viol = 0
worst = []
d = START
while d <= END:
    c = cutoff_utc(d)
    for t in target_hours_utc(d):
        pairs += 1
        v = audit(FEATURES_MODEL_B, c, t)
        if v:
            viol += 1
            worst.extend(v[:1])
    d = date.fromordinal(d.toordinal() + 1)
print(f"  cap kiem tra : {pairs:,}")
print(f"  vi pham      : {viol:,}")
print(f"  ket qua      : {'PASS - khong feature nao biet truoc cutoff' if viol == 0 else 'FAIL'}")
for w in worst[:5]:
    print("   ", w)

print("\n=== Mot row mau (2026-07-01, gio peak) ===")
row = (feats.filter("forecast_date = date'2026-07-01' AND target_local_hour = 18")
       .first())
if row:
    d = row.asDict()
    for k in ("forecast_date", "cutoff_utc", "target_timestamp_utc", "hours_ahead",
              "target_local_hour", "is_weekend", "is_holiday",
              "demand_cutoff_minus_3h", "demand_rolling_mean_24h_to_cutoff",
              "demand_same_hour_minus_2d", "demand_same_hour_minus_7d",
              "demand_same_hour_prev_week_mean",
              "temp_c_region_mean_lead2", "cdd_region_lead2", "hdd_region_lead2",
              "temp_c_target_day_max_lead2", "cdd_target_day_sum_lead2",
              "label_demand_mwh", "benchmark_eia_df_mwh"):
        v = d.get(k)
        print(f"  {k:<38} {v if not isinstance(v, float) else f'{v:,.2f}'}")
spark.stop()

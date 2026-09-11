#!/usr/bin/env python3
"""Silver -> Gold locally, and report what the anomaly filter actually prevented."""
from __future__ import annotations
import sys
from datetime import date
from pyspark.sql import functions as F
sys.path.insert(0, ".")
from src.gold.daily_summary import daily_summary, peak_history   # noqa: E402
from src.silver.calendar_utils import calendar_frame             # noqa: E402
from src.silver.electricity import build_silver                  # noqa: E402
from src.spark import local_session                              # noqa: E402

spark = local_session(app="gold-local")
silver, _ = build_silver(spark.read.parquet("data/landing/eia/region-data"), spark)
silver.cache()
# One day earlier than the UTC range: 2019-01-01T00:00Z is 2018-12-31 19:00 local, so
# the first local operating day starts before the data does.
cal = calendar_frame(spark, date(2018, 12, 31), date(2026, 12, 31))

filt = daily_summary(silver, cal, exclude_suspect=True).cache()
unfilt = daily_summary(silver, cal, exclude_suspect=False).cache()
print(f"gold daily rows  {filt.count():>8,}")

print("\n=== PEAK MOI THOI DAI: co filter vs khong ===")
for name, df in (("KHONG filter", unfilt), ("CO filter", filt)):
    top = (df.filter("is_complete").orderBy(F.desc("peak_demand_mwh"))
             .select("local_date", "peak_demand_mwh", "peak_local_hour").limit(5).collect())
    print(f"\n  {name}:")
    for r in top:
        print(f"    {r['local_date']}  {r['peak_demand_mwh']:>10,.0f} MWh  h={r['peak_local_hour']:02d}")
print("\n  PJM ky luc that ~165,563 MW (8/2006). Recent summer peak ~150-155 GW.")

print("\n=== Peak theo nam (co filter, ngay complete) ===")
(filt.filter("is_complete").groupBy("year")
 .agg(F.max("peak_demand_mwh").alias("peak_mwh"),
      F.round(F.avg("load_factor"), 3).alias("avg_load_factor"),
      F.count("*").alias("complete_days"))
 .orderBy("year").show(truncate=False))

print("=== Benchmark EIA DF: sai so peak (chi ngay complete) ===")
(filt.filter("is_complete AND day_ahead_peak_mwh IS NOT NULL")
 .agg(F.count("*").alias("days"),
      F.round(F.avg("peak_magnitude_error_pct"), 3).alias("bias_pct"),
      F.round(F.avg(F.abs("peak_magnitude_error_pct")), 3).alias("mape_peak_pct"),
      F.round(F.avg(F.abs("peak_timing_error_hours")), 3).alias("mae_timing_h"),
      F.round(100 * F.avg((F.col("peak_timing_error_hours") == 0).cast("int")), 1)
        .alias("timing_hit_pct")).show(truncate=False))

print("=== Do phu ngay complete ===")
(filt.groupBy("year").agg(
    F.count("*").alias("days"),
    F.sum(F.col("is_complete").cast("int")).alias("complete"),
    F.round(100*F.avg(F.col("is_complete").cast("int")), 1).alias("pct"))
 .orderBy("year").show(truncate=False))
spark.stop()

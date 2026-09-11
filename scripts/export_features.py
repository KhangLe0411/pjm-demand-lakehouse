#!/usr/bin/env python3
"""Spark -> Parquet. The only place the ML path touches Spark.

Everything downstream reads this with pandas. The backtest is 55 folds of single-node
XGBoost over ~48k rows, which Spark would only slow down, and keeping `src/ml/` free of
Spark means its tests run in seconds instead of paying JVM startup each time.

On Databricks the equivalent read is one line:
    spark.table("energy.gold.demand_features").toPandas()
"""
from __future__ import annotations

import sys
from datetime import date

sys.path.insert(0, ".")
from src.features.build import build_features  # noqa: E402
from src.silver.electricity import build_silver  # noqa: E402
from src.silver.weather import build_weather_silver  # noqa: E402
from src.spark import local_session  # noqa: E402

OUT = "data/features/demand_features"
spark = local_session(app="export-features")
silver, _ = build_silver(spark.read.parquet("data/landing/eia/region-data"), spark)
weather, _ = build_weather_silver(spark.read.parquet("data/landing/weather"))
feats = build_features(silver, weather, spark, date(2021, 3, 24), date(2026, 9, 6),
                       weather_leads=(1, 2))   # lead 1 for the D-21 sensitivity only

# Written by Spark and read back with pyarrow rather than going through toPandas():
# PySpark 3.5's Arrow path imports distutils, which Python 3.12 removed. Round-tripping
# through Parquet also mirrors what the real pipeline does - Gold is a table, and the
# ML step reads it - so the local path and the Databricks path stay the same shape.
feats.coalesce(1).write.mode("overwrite").parquet(OUT)
spark.stop()

import pandas as pd  # noqa: E402

pdf = pd.read_parquet(OUT)
print(f"{len(pdf):,} rows x {len(pdf.columns)} cols -> {OUT}")
print(f"labelled rows : {pdf['label_demand_mwh'].notna().sum():,}")
print(f"benchmark rows: {pdf['benchmark_eia_df_mwh'].notna().sum():,}")
print(f"forecast dates: {pdf['forecast_date'].min()} -> {pdf['forecast_date'].max()}")

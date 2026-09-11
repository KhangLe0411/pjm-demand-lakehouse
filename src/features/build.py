"""Build the day-ahead feature table.

The joins are generated from `spec.FEATURES_*` rather than written out by hand. That
correspondence is the point: the leakage audit checks the declarations, so if the
implementation could drift from them the audit would be verifying a document instead of
the data. `tests/test_features_build.py` asserts every declared feature exists as a
column and that no undeclared feature sneaks in.

Grain: one row per (forecast_date, target hour). ~1,990 forecast dates x ~24 hours.
"""
from __future__ import annotations

from datetime import date, timedelta

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from src.features.calendar_features import calendar_features
from src.features.contract import cutoff_utc, target_hours_utc
from src.features.spec import (
    FEATURES_MODEL_B,
    PRIMARY_LEAD,
    PUBLICATION_LAG_HOURS,
    Anchor,
    FeatureSpec,
)
from src.silver.calendar_utils import PJM_TZ

ROLLING_ANCHOR_H = int(PUBLICATION_LAG_HOURS["D"])   # newest demand usable at cutoff
PREV_WEEK_DAYS = (2, 3, 4, 5, 6, 7)                  # same clock hour, 2..7 days back


def contract_frame(spark: SparkSession, start: date, end: date,
                   tz: str = PJM_TZ) -> DataFrame:
    """(forecast_date, cutoff_utc, target_timestamp_utc) plus the target's calendar.

    Generated in Python because both the cutoff instant and the target day's length
    depend on tz rules Spark's date functions do not expose.
    """
    rows, d = [], start
    while d <= end:
        c = cutoff_utc(d, tz)
        for t in target_hours_utc(d, tz):
            local = t.astimezone(__import__("zoneinfo").ZoneInfo(tz))
            cf = calendar_features(local.date())
            rows.append((
                d, c.replace(tzinfo=None), t.replace(tzinfo=None),
                local.date(), local.hour,
                round((t - c).total_seconds() / 3600, 2),
                cf["day_of_week"], cf["month"], cf["day_of_year"],
                cf["is_weekend"], cf["is_holiday"], cf["days_to_nearest_holiday"],
            ))
        d += timedelta(days=1)
    return spark.createDataFrame(rows, [
        "forecast_date", "cutoff_utc", "target_timestamp_utc",
        "target_local_date", "target_local_hour", "hours_ahead",
        "day_of_week", "month", "day_of_year",
        "is_weekend", "is_holiday", "days_to_nearest_holiday"])


def _demand_lookup(silver: DataFrame) -> DataFrame:
    """Demand keyed by hour, with flagged readings nulled.

    Training on a value the pipeline already doubts would teach the model the fault.
    The flag is kept alongside so the label side can report what it dropped.
    """
    clean = F.when(~F.col("is_demand_anomaly_suspect")
                   & ~F.col("is_above_historical_record"),
                   F.col("actual_demand_mwh"))
    roll = (Window.partitionBy("region_id")
            .orderBy(F.col("event_timestamp_utc").cast("long"))
            .rangeBetween(-23 * 3600, 0))   # absolute seconds: DST-safe by construction
    return (
        silver.withColumn("demand_clean", clean)
        .withColumn("roll_mean_24h", F.avg("demand_clean").over(roll))
        .withColumn("roll_std_24h", F.stddev("demand_clean").over(roll))
        .select("event_timestamp_utc", "demand_clean", "roll_mean_24h", "roll_std_24h",
                F.col("day_ahead_forecast_mwh").alias("eia_df_mwh"),
                F.col("is_demand_anomaly_suspect").alias("suspect"),
                F.col("is_above_historical_record").alias("above_record"))
    )


def _join_lag(df: DataFrame, lookup: DataFrame, anchor_col: str,
              offset_h: float, out_col: str, src: str = "demand_clean") -> DataFrame:
    key = (F.col(anchor_col).cast("long") - int(offset_h * 3600)).cast("timestamp")
    small = lookup.select(F.col("event_timestamp_utc").alias("_ts"),
                          F.col(src).alias(out_col))
    return (df.withColumn("_key", key)
            .join(F.broadcast(small), F.col("_key") == F.col("_ts"), "left")
            .drop("_key", "_ts"))


def _weather_wide(weather: DataFrame, lead: int = PRIMARY_LEAD) -> DataFrame:
    """One row per valid hour: temperature per point, plus region aggregates."""
    w = weather.filter(F.col("forecast_lead_days") == lead)
    per_point = [
        F.max(F.when(F.col("point_id") == p, F.col("temperature_c")))
         .alias(f"temp_c_{p}_lead{lead}")
        for p in sorted({r["point_id"] for r in w.select("point_id").distinct().collect()})
    ]
    return (
        w.groupBy(F.col("valid_timestamp_utc").alias("_wts")).agg(
            *per_point,
            F.avg("temperature_c").alias(f"temp_c_region_mean_lead{lead}"),
            F.avg("cdd_c").alias(f"cdd_region_lead{lead}"),
            F.avg("hdd_c").alias(f"hdd_region_lead{lead}"),
        )
    )


def _weather_daily(weather: DataFrame, tz: str = PJM_TZ,
                   lead: int = PRIMARY_LEAD) -> DataFrame:
    """Target-day shape. A day's peak load tracks the day's temperature extreme more
    closely than any single hour, so the daily aggregate is not redundant with hourly."""
    w = (weather.filter(F.col("forecast_lead_days") == lead)
         .withColumn("_ld", F.to_date(F.from_utc_timestamp("valid_timestamp_utc", tz))))
    region = w.groupBy("_ld", "valid_timestamp_utc").agg(
        F.avg("temperature_c").alias("t"), F.avg("cdd_c").alias("c"),
        F.avg("hdd_c").alias("h"))
    # SUM also skips nulls, so a day missing several hours would report a smaller total
    # that looks like a milder day rather than an incomplete one. The daily aggregates
    # are therefore emitted only when the day is whole.
    whole = F.count("t") == F.count(F.lit(1))
    return region.groupBy(F.col("_ld").alias("_dld")).agg(
        F.when(whole, F.max("t")).alias(f"temp_c_target_day_max_lead{lead}"),
        F.when(whole, F.min("t")).alias(f"temp_c_target_day_min_lead{lead}"),
        F.when(whole, F.sum("c")).alias(f"cdd_target_day_sum_lead{lead}"),
        F.when(whole, F.sum("h")).alias(f"hdd_target_day_sum_lead{lead}"))


def build_features(silver_elec: DataFrame, silver_weather: DataFrame | None,
                   spark: SparkSession, start: date, end: date,
                   specs: list[FeatureSpec] = FEATURES_MODEL_B,
                   tz: str = PJM_TZ,
                   weather_leads: tuple[int, ...] = (PRIMARY_LEAD,)) -> DataFrame:
    """`weather_leads` emits one column set per vintage.

    Default is the primary lead alone. Passing (1, 2) additionally materialises the
    lead-1 columns for the sensitivity analysis promised in D-21 — measuring what the
    provable-availability margin costs. Those columns exist to be *measured*, not
    deployed: `audit()` rejects them, and a test asserts that it does.
    """
    df = contract_frame(spark, start, end, tz)
    # Not cached here. `.cache()` is unsupported on Databricks serverless
    # (NOT_SUPPORTED_WITH_SERVERLESS), and a library has no business assuming an engine
    # capability — caching is the caller's decision, and the local scripts do it. At
    # ~67k rows the recomputation this costs is negligible.
    lookup = _demand_lookup(silver_elec)

    # Simple lags, driven straight off the declarations.
    for s in specs:
        if s.anchor is Anchor.CUTOFF and s.name.startswith("demand_cutoff_minus_"):
            df = _join_lag(df, lookup, "cutoff_utc", s.offset_hours, s.name)
        elif s.anchor is Anchor.TARGET and s.name.startswith("demand_same_hour_minus_"):
            df = _join_lag(df, lookup, "target_timestamp_utc", s.offset_hours, s.name)

    # Rolling window: its newest input is cutoff - publication lag, matching the spec.
    df = _join_lag(df, lookup, "cutoff_utc", ROLLING_ANCHOR_H,
                   "demand_rolling_mean_24h_to_cutoff", "roll_mean_24h")
    df = _join_lag(df, lookup, "cutoff_utc", ROLLING_ANCHOR_H,
                   "demand_rolling_std_24h_to_cutoff", "roll_std_24h")

    # Same clock hour, 2..7 days back. Averaged from explicit lags rather than a window,
    # so each contributing offset is individually auditable.
    parts = []
    for d in PREV_WEEK_DAYS:
        col = f"_pw_{d}"
        df = _join_lag(df, lookup, "target_timestamp_utc", 24 * d, col)
        parts.append(F.col(col))
    df = (df.withColumn("demand_same_hour_prev_week_mean",
                        sum(F.coalesce(p, F.lit(0.0)) for p in parts)
                        / F.greatest(sum(p.isNotNull().cast("int") for p in parts),
                                     F.lit(1)))
          .drop(*[f"_pw_{d}" for d in PREV_WEEK_DAYS]))

    if silver_weather is not None:
        for lead in weather_leads:
            wide = _weather_wide(silver_weather, lead)
            daily = _weather_daily(silver_weather, tz, lead)
            df = (df.join(F.broadcast(wide),
                          F.col("target_timestamp_utc") == F.col("_wts"), "left")
                    .drop("_wts")
                    .join(F.broadcast(daily),
                          F.col("target_local_date") == F.col("_dld"), "left")
                    .drop("_dld"))

    # Label, and the benchmark it will be scored against. Neither is a feature.
    label = lookup.select(F.col("event_timestamp_utc").alias("_lts"),
                          F.col("demand_clean").alias("label_demand_mwh"),
                          F.col("eia_df_mwh").alias("benchmark_eia_df_mwh"),
                          F.col("suspect").alias("label_is_suspect"),
                          F.col("above_record").alias("label_above_record"))
    return (df.join(F.broadcast(label),
                    F.col("target_timestamp_utc") == F.col("_lts"), "left")
            .drop("_lts")
            .withColumn("target_local_hour", F.col("target_local_hour")))

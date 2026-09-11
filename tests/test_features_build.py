"""Spec/implementation correspondence.

The leakage audit checks declarations. If the built table could hold columns the
declarations do not cover, or miss ones they do, the audit would be verifying a document
rather than the data. These tests close that gap.
"""
from datetime import date

from pyspark.sql import functions as F

from src.features.build import build_features
from src.features.spec import FEATURES_MODEL_A, FEATURES_MODEL_B, Anchor

START, END = date(2024, 6, 1), date(2024, 6, 10)

SILVER_COLS = ["region_id", "event_timestamp_utc", "local_date", "local_hour",
               "actual_demand_mwh", "day_ahead_forecast_mwh",
               "is_demand_anomaly_suspect", "is_above_historical_record",
               "is_missing_hour"]


def silver_elec(spark, start=date(2024, 4, 1), end=date(2024, 7, 1)):
    """A continuous hourly series with a recognisable daily shape."""
    rows, cur = [], start
    while cur <= end:
        for h in range(24):
            dem = 100_000 + 20_000 * (1 if 14 <= h <= 20 else 0) + h * 50
            rows.append(("PJM", f"{cur.isoformat()} {h:02d}:00:00", cur, h,
                         float(dem), float(dem) * 0.99, False, False, False))
        cur = date.fromordinal(cur.toordinal() + 1)
    return (spark.createDataFrame(rows, SILVER_COLS)
            .withColumn("event_timestamp_utc", F.to_timestamp("event_timestamp_utc")))


def test_every_declared_model_a_feature_is_a_column(spark):
    df = build_features(silver_elec(spark), None, spark, START, END,
                        specs=FEATURES_MODEL_A)
    missing = {s.name for s in FEATURES_MODEL_A} - set(df.columns)
    assert not missing, f"declared but not built: {sorted(missing)}"


def test_grain_is_one_row_per_forecast_date_and_target_hour(spark):
    df = build_features(silver_elec(spark), None, spark, START, END,
                        specs=FEATURES_MODEL_A)
    assert df.count() == 10 * 24
    assert df.select("forecast_date", "target_timestamp_utc").distinct().count() == 10 * 24


def test_label_and_benchmark_are_present_and_are_not_features(spark):
    df = build_features(silver_elec(spark), None, spark, START, END,
                        specs=FEATURES_MODEL_A)
    assert {"label_demand_mwh", "benchmark_eia_df_mwh"} <= set(df.columns)
    names = {s.name for s in FEATURES_MODEL_B}
    assert "label_demand_mwh" not in names
    assert "benchmark_eia_df_mwh" not in names


def test_hours_ahead_is_carried_for_evaluation_but_not_declared(spark):
    """D-05: under a fixed cutoff it is a deterministic function of the target hour, so
    it is collinear with `target_local_hour` and belongs in evaluation, not features."""
    df = build_features(silver_elec(spark), None, spark, START, END,
                        specs=FEATURES_MODEL_A)
    assert "hours_ahead" in df.columns
    assert "hours_ahead" not in {s.name for s in FEATURES_MODEL_B}


def test_cutoff_anchored_lag_reads_the_declared_hour(spark):
    """cutoff-3h must be exactly cutoff minus three hours of demand, not an approximation."""
    s = silver_elec(spark)
    df = build_features(s, None, spark, START, START, specs=FEATURES_MODEL_A)
    r = df.orderBy("target_timestamp_utc").first()
    expected = (s.filter(F.col("event_timestamp_utc")
                         == F.lit(r["cutoff_utc"]) - F.expr("INTERVAL 3 HOURS"))
                .first()["actual_demand_mwh"])
    assert r["demand_cutoff_minus_3h"] == expected


def test_a_flagged_reading_never_reaches_a_feature_or_the_label(spark):
    """Training on a value the pipeline already doubts would teach it the fault."""
    s = silver_elec(spark)
    poisoned = s.withColumn(
        "is_demand_anomaly_suspect",
        F.col("event_timestamp_utc") == F.to_timestamp(F.lit("2024-06-02 18:00:00")))
    df = build_features(poisoned, None, spark, START, END, specs=FEATURES_MODEL_A)
    hit = df.filter(F.col("target_timestamp_utc")
                    == F.to_timestamp(F.lit("2024-06-02 18:00:00"))).first()
    assert hit["label_demand_mwh"] is None
    assert hit["label_is_suspect"] is True
    # and it must not survive as a lagged feature two days later either
    later = df.filter(F.col("target_timestamp_utc")
                      == F.to_timestamp(F.lit("2024-06-04 18:00:00"))).first()
    assert later["demand_same_hour_minus_2d"] is None


def test_prev_week_mean_averages_only_the_offsets_that_exist(spark):
    df = build_features(silver_elec(spark), None, spark, START, END,
                        specs=FEATURES_MODEL_A)
    vals = [r["demand_same_hour_prev_week_mean"] for r in df.collect()]
    assert all(v is not None and 90_000 < v < 145_000 for v in vals)


def test_dst_days_produce_23_and_25_target_rows(spark):
    s = silver_elec(spark, date(2026, 2, 1), date(2026, 4, 1))
    spring = build_features(s, None, spark, date(2026, 3, 7), date(2026, 3, 7),
                            specs=FEATURES_MODEL_A)
    assert spring.count() == 23
    s2 = silver_elec(spark, date(2026, 10, 1), date(2026, 11, 15))
    fall = build_features(s2, None, spark, date(2026, 10, 31), date(2026, 10, 31),
                          specs=FEATURES_MODEL_A)
    assert fall.count() == 25


def test_library_code_does_not_assume_engine_capabilities():
    """`.cache()` / `.persist()` raise NOT_SUPPORTED_WITH_SERVERLESS on Databricks
    serverless. A transform library must run on whatever engine the caller has, so the
    decision to cache belongs to the caller — scripts/ may, src/ may not.

    Found the hard way: one `.cache()` in build_features failed the features task in the
    deployed pipeline while every local test passed.
    """
    import pathlib
    offenders = []
    for f in pathlib.Path("src").rglob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if ".cache()" in code or ".persist(" in code:
                offenders.append(f"{f}:{i}")
    assert not offenders, f"engine-specific calls in library code: {offenders}"

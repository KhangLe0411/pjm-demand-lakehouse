from pyspark.sql import functions as F
from pyspark.sql.types import (
    DoubleType, IntegerType, StringType, StructField, StructType,
)

from src.silver.weather import DEGREE_DAY_BASE_C, add_degree_days, build_weather_silver

# Declared, not inferred: rows exercising the pre-2024 case carry all-NULL measure
# columns, which gives Spark nothing to infer a type from.
LANDING_SCHEMA = StructType([
    StructField("valid_timestamp_utc", StringType()),
    StructField("forecast_lead_days", IntegerType()),
    StructField("region_id", StringType()),
    StructField("point_id", StringType()),
    StructField("latitude", DoubleType()),
    StructField("longitude", DoubleType()),
    *[StructField(c, DoubleType()) for c in (
        "temperature_c", "apparent_temperature_c", "dew_point_c",
        "relative_humidity_pct", "wind_speed_kmh", "precipitation_mm")],
    StructField("source", StringType()),
    StructField("source_file", StringType()),
    StructField("ingested_at", StringType()),
])
COLS = [f.name for f in LANDING_SCHEMA.fields]


def landing(spark, rows):
    """rows: (valid_ts, lead, point, temp, humidity, ingested_at)"""
    return spark.createDataFrame(
        [(ts, lead, "PJM", p, 39.95, -75.17, temp, temp, temp - 5.0, hum, 10.0, 0.0,
          "test", "f.parquet", ing) for ts, lead, p, temp, hum, ing in rows],
        LANDING_SCHEMA)


def test_cooling_and_heating_degrees_are_mutually_exclusive(spark):
    df = add_degree_days(landing(spark, [
        ("2024-07-01T12:00", 2, "philadelphia_peco", 30.0, 60.0, "t"),
        ("2024-01-01T12:00", 2, "philadelphia_peco", 0.0, 60.0, "t"),
        ("2024-04-01T12:00", 2, "philadelphia_peco", DEGREE_DAY_BASE_C, 60.0, "t"),
    ]).withColumn("temperature_c", F.col("temperature_c")))
    got = {r["valid_timestamp_utc"]: (round(r["cdd_c"], 3), round(r["hdd_c"], 3))
           for r in df.collect()}
    assert got["2024-07-01T12:00"] == (11.667, 0.0)     # hot: cooling only
    assert got["2024-01-01T12:00"] == (0.0, 18.333)     # cold: heating only
    assert got["2024-04-01T12:00"] == (0.0, 0.0)        # at base: neither


def test_latest_vintage_wins(spark):
    s, _ = build_weather_silver(landing(spark, [
        ("2024-07-01T12:00", 2, "philadelphia_peco", 25.0, 60.0, "2026-01-01T00:00"),
        ("2024-07-01T12:00", 2, "philadelphia_peco", 27.0, 65.0, "2026-01-02T00:00"),
    ]))
    assert s.count() == 1
    assert s.first()["temperature_c"] == 27.0


def test_leads_are_distinct_rows_not_duplicates(spark):
    s, _ = build_weather_silver(landing(spark, [
        ("2024-07-01T12:00", 1, "philadelphia_peco", 25.0, 60.0, "t"),
        ("2024-07-01T12:00", 2, "philadelphia_peco", 27.0, 60.0, "t"),
    ]))
    assert s.count() == 2
    assert {r["forecast_lead_days"] for r in s.collect()} == {1, 2}


def test_out_of_bounds_nulls_only_the_offending_column(spark):
    """A humidity fault must not cost the temperature reading: temperature carries the
    longer history the primary model needs."""
    s, _ = build_weather_silver(landing(spark, [
        ("2024-07-01T12:00", 2, "philadelphia_peco", 25.0, 999.0, "t"),
    ]))
    r = s.first()
    assert r["relative_humidity_pct"] is None
    assert r["temperature_c"] == 25.0
    assert r["out_of_bounds_columns"] == ["relative_humidity_pct"]


def test_pre_2024_rows_keep_temperature_despite_null_companions(spark):
    """Humidity, wind and precipitation are absent before 2024-01-19 by design."""
    df = spark.createDataFrame(
        [("2022-06-01T12:00", 2, "PJM", "philadelphia_peco", 39.95, -75.17,
          22.0, None, None, None, None, None, "test", "f.parquet", "t")],
        LANDING_SCHEMA)
    s, q = build_weather_silver(df)
    assert q.count() == 0
    r = s.first()
    assert r["temperature_c"] == 22.0 and r["relative_humidity_pct"] is None
    assert r["cdd_c"] is not None


def test_missing_temperature_does_not_become_zero_degree_days(spark):
    """F.greatest skips nulls, so `greatest(NULL - base, 0)` returns 0.0. Unguarded,
    a missing temperature reads as "zero cooling degrees" - i.e. a mild hour - and the
    model trains on a value that was never measured."""
    df = spark.createDataFrame(
        [("2022-06-01T12:00", 2, "PJM", "philadelphia_peco", 39.95, -75.17,
          None, None, None, None, None, None, "test", "f.parquet", "t")],
        LANDING_SCHEMA)
    r = add_degree_days(df).first()
    assert r["cdd_c"] is None, "missing temperature leaked in as CDD=0"
    assert r["hdd_c"] is None


def test_degree_days_coverage_can_never_exceed_temperature_coverage(spark):
    s, _ = build_weather_silver(spark.createDataFrame([
        ("2022-06-01T12:00", 2, "PJM", "p1", 39.9, -75.1, 25.0, None, None, None, None, None, "t", "f", "t"),
        ("2022-06-01T13:00", 2, "PJM", "p1", 39.9, -75.1, None, None, None, None, None, None, "t", "f", "t"),
    ], LANDING_SCHEMA))
    rows = s.collect()
    n_temp = sum(r["temperature_c"] is not None for r in rows)
    n_cdd = sum(r["cdd_c"] is not None for r in rows)
    assert n_cdd == n_temp == 1

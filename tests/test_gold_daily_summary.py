"""Gold peak contracts. The recurring theme: a peak must never be established by a
reading the pipeline already doubts, nor by a day that barely reported."""
from datetime import date

from pyspark.sql import functions as F

from src.gold.daily_summary import daily_summary, peak_history
from src.silver.calendar_utils import calendar_frame

SILVER_COLS = ["region_id", "event_timestamp_utc", "local_date", "local_hour",
               "actual_demand_mwh", "day_ahead_forecast_mwh",
               "is_demand_anomaly_suspect", "is_missing_hour",
               "is_above_historical_record"]


def silver(spark, rows):
    """rows: (local_date, local_hour, demand, forecast, suspect)"""
    data = [("PJM", f"{d.isoformat()} {h:02d}:00:00", d, h, dem, fc, sus,
             dem is None, False) for d, h, dem, fc, sus in rows]
    return (spark.createDataFrame(data, SILVER_COLS)
            .withColumn("event_timestamp_utc", F.to_timestamp("event_timestamp_utc")))


def cal(spark, d0, d1):
    return calendar_frame(spark, d0, d1)


def full_day(d, n, demand_at=None, suspect_at=None, base=100_000):
    """A whole operating day at `base`, optionally overriding one hour."""
    out = []
    for h in range(n):
        dem = base + h * 100
        sus = False
        if demand_at and h == demand_at[0]:
            dem = demand_at[1]
        if suspect_at and h == suspect_at[0]:
            dem, sus = suspect_at[1], True
        out.append((d, h, float(dem), float(dem) * 0.99, sus))
    return out


# ------------------------------------------------------------------- suspect rows

def test_peak_ignores_a_suspect_reading(spark):
    """262,651 is the real 2020-07-24 value: inside the hard bound, still wrong. Left
    in, it becomes the published all-time peak."""
    d = date(2024, 6, 3)
    df = daily_summary(silver(spark, full_day(d, 24, suspect_at=(14, 262_651))),
                       cal(spark, d, d)).first()
    assert df["peak_demand_mwh"] == 102_300.0     # hour 23, the honest maximum
    assert df["peak_local_hour"] == 23
    assert df["suspect_hours"] == 1


def test_measuring_the_filter_shows_what_it_prevented(spark):
    d = date(2024, 6, 3)
    rows = full_day(d, 24, suspect_at=(14, 262_651))
    kept = daily_summary(silver(spark, rows), cal(spark, d, d),
                         exclude_suspect=False).first()
    assert kept["peak_demand_mwh"] == 262_651.0   # what publishing unfiltered costs
    assert kept["peak_local_hour"] == 14


def test_a_suspect_hour_is_excluded_from_demand_hours(spark):
    d = date(2024, 6, 3)
    r = daily_summary(silver(spark, full_day(d, 24, suspect_at=(14, 262_651))),
                      cal(spark, d, d)).first()
    assert r["demand_hours"] == 23
    assert r["is_complete"] is False              # 23 usable of 24 expected


# ----------------------------------------------------------------------- DST days

def test_fall_back_day_considers_all_25_hours(spark):
    d = date(2026, 11, 1)
    rows = full_day(d, 25, demand_at=(24, 150_000))
    r = daily_summary(silver(spark, rows), cal(spark, d, d)).first()
    assert r["expected_hours"] == 25
    assert r["actual_hours"] == 25
    assert r["peak_demand_mwh"] == 150_000.0      # the 25th hour can hold the peak
    assert r["is_complete"] is True


def test_spring_forward_day_of_23_hours_is_complete(spark):
    d = date(2026, 3, 8)
    r = daily_summary(silver(spark, full_day(d, 23)), cal(spark, d, d)).first()
    assert (r["expected_hours"], r["actual_hours"]) == (23, 23)
    assert r["is_complete"] is True


def test_a_24_hour_day_on_a_23_hour_date_is_not_complete(spark):
    """Guards against reverting to a hard-coded 24."""
    d = date(2026, 3, 8)
    r = daily_summary(silver(spark, full_day(d, 24)), cal(spark, d, d)).first()
    assert r["expected_hours"] == 23 and r["actual_hours"] == 24
    assert r["is_complete"] is False


# -------------------------------------------------------------------- peak metrics

def test_peak_hour_is_local_not_utc(spark):
    d = date(2024, 6, 3)
    rows = full_day(d, 24, demand_at=(18, 140_000))
    r = daily_summary(silver(spark, rows), cal(spark, d, d)).first()
    assert r["peak_local_hour"] == 18


def test_forecast_peak_is_found_independently_of_actual_peak(spark):
    """The BA can peak at a different hour than reality; timing error is the point."""
    d = date(2024, 6, 3)
    rows = [(d, h, 100_000.0, 100_000.0, False) for h in range(24)]
    rows[18] = (d, 18, 140_000.0, 100_000.0, False)   # actual peaks at 18
    rows[19] = (d, 19, 100_000.0, 138_000.0, False)   # forecast peaks at 19
    r = daily_summary(silver(spark, rows), cal(spark, d, d)).first()
    assert (r["peak_local_hour"], r["day_ahead_peak_local_hour"]) == (18, 19)
    assert r["peak_timing_error_hours"] == 1
    assert r["peak_magnitude_error_mwh"] == -2000.0   # signed: under-forecast


def test_peak_error_sign_survives_aggregation(spark):
    """A signed error keeps a persistent bias visible; abs() would hide it."""
    d0, d1 = date(2024, 6, 3), date(2024, 6, 4)
    rows = ([(d0, h, 100_000.0, 95_000.0, False) for h in range(24)]
            + [(d1, h, 100_000.0, 96_000.0, False) for h in range(24)])
    got = daily_summary(silver(spark, rows), cal(spark, d0, d1)) \
        .agg(F.avg("peak_magnitude_error_mwh").alias("bias")).first()
    assert got["bias"] < 0            # consistently under-forecasting


# ------------------------------------------------------------------ peak history

def test_incomplete_days_cannot_set_a_record(spark):
    """A day reporting two hours must not own the all-time peak."""
    d0, d1 = date(2024, 6, 3), date(2024, 6, 4)
    rows = (full_day(d0, 24)
            + [(d1, 12, 200_000.0, 190_000.0, False)])     # 1 hour of 24
    daily = daily_summary(silver(spark, rows), cal(spark, d0, d1))
    hist = {r["local_date"]: r for r in peak_history(daily).collect()}
    assert d1 not in hist
    assert hist[d0]["running_peak_mwh"] == 102_300.0


def test_absolute_ceiling_catches_what_the_relative_detector_missed(spark):
    """176,085 MWh on 2020-07-29 scored 1.26 against its neighbours - under the 1.30
    threshold - yet exceeds PJM's all-time peak. Only an absolute check finds it."""
    d = date(2020, 7, 29)
    rows = [(d, h, 130_000.0, 129_000.0, False) for h in range(24)]
    rows[21] = (d, 21, 176_085.0, 129_000.0, False)   # suspect flag deliberately False
    s = silver(spark, rows).drop("is_above_historical_record").withColumn(
        "is_above_historical_record", F.col("actual_demand_mwh") > 165_563.0)
    r = daily_summary(s, cal(spark, d, d)).first()
    assert r["above_record_hours"] == 1
    assert r["peak_demand_mwh"] == 130_000.0       # the record-breaker is not the peak

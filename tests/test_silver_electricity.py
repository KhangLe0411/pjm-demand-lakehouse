"""Silver contract tests. Every case here corresponds to something the real EIA feed
actually contains, established by profiling the 2019-2026 backfill."""
from datetime import datetime

import pytest
from pyspark.sql import functions as F
from pyspark.sql.types import StringType, StructField, StructType

from src.silver.electricity import (
    add_local_time, align_day_ahead_forecast, attach_hour_spine, build_silver,
    classify, flag_anomalies, pivot_to_wide, resolve_revisions, split_quarantine,
)

BRONZE_COLS = ["period", "respondent", "respondent_name", "type", "type_name",
               "value", "value_units", "source", "source_file", "ingested_at"]
# Every Bronze column is a string, exactly as the extractor writes it. Declared rather
# than inferred because a single row with value=None gives Spark nothing to infer from.
BRONZE_SCHEMA = StructType([StructField(c, StringType(), True) for c in BRONZE_COLS])


def bronze(spark, rows):
    """rows: (period, type, value, ingested_at)"""
    return spark.createDataFrame(
        [(p, "PJM", "PJM Interconnection, LLC", t, t, v, "megawatthours",
          "test", "f.parquet", ts) for p, t, v, ts in rows],
        BRONZE_SCHEMA)


def reason_for(spark, rows):
    q = classify(resolve_revisions(bronze(spark, rows)))
    return {(r["period"], r["type"]): r["reason"] for r in q.collect()}


# ----------------------------------------------------------------- revisions

def test_exact_duplicate_collapses_without_counting_as_revision(spark):
    """The backfill produced 2,976 of these: same key, same value, two ingest runs."""
    df = resolve_revisions(bronze(spark, [
        ("2026-08-01T00", "D", "127017", "2026-09-08T07:24:06+00:00"),
        ("2026-08-01T00", "D", "127017", "2026-09-08T07:24:23+00:00"),
    ]))
    assert df.count() == 1
    assert df.first()["revision_count"] == 0


def test_revision_keeps_latest_vintage_and_is_counted(spark):
    df = resolve_revisions(bronze(spark, [
        ("2026-08-01T00", "D", "95000", "2026-09-08T11:00:00+00:00"),
        ("2026-08-01T00", "D", "95700", "2026-09-08T14:00:00+00:00"),
    ]))
    row = df.first()
    assert df.count() == 1
    assert row["value"] == "95700"        # newest wins
    assert row["revision_count"] == 1


def test_revisions_resolve_before_validation_so_a_fix_is_honoured(spark):
    """A value corrected upstream must not leave its superseded self in quarantine."""
    reasons = reason_for(spark, [
        ("2026-08-01T00", "D", "2147480000", "2026-09-08T11:00:00+00:00"),
        ("2026-08-01T00", "D", "95700",      "2026-09-08T14:00:00+00:00"),
    ])
    assert reasons == {("2026-08-01T00", "D"): None}


# ---------------------------------------------------------------- validation

def test_null_value_is_reported_absence_not_a_defect(spark):
    """233 rows in the real feed carry a NULL value. They are known gaps, so they flow
    to Silver as NULL rather than being quarantined."""
    assert reason_for(spark, [("2020-05-01T03", "D", None, "t")]) \
        == {("2020-05-01T03", "D"): None}


def test_unparseable_value_is_quarantined(spark):
    assert reason_for(spark, [("2020-05-01T03", "D", "n/a", "t")]) \
        == {("2020-05-01T03", "D"): "INVALID_NUMERIC"}


def test_int32_sentinel_is_quarantined(spark):
    """2021-10-19T03..05 in the real feed: D and NG both near INT32_MAX."""
    assert reason_for(spark, [("2021-10-19T04", "D", "2147480000", "t")]) \
        == {("2021-10-19T04", "D"): "IMPOSSIBLE_VALUE"}


def test_negative_interchange_is_valid(spark):
    """1,627 real TI readings are negative. A blanket `value >= 0` would destroy them."""
    assert reason_for(spark, [("2020-01-01T00", "TI", "-4456", "t")]) \
        == {("2020-01-01T00", "TI"): None}


def test_negative_demand_is_not(spark):
    assert reason_for(spark, [("2020-01-01T00", "D", "-4456", "t")]) \
        == {("2020-01-01T00", "D"): "IMPOSSIBLE_VALUE"}


@pytest.mark.parametrize("period,want", [
    ("not-a-timestamp", "INVALID_TIMESTAMP"),
    ("2020-01-01T00", None),
])
def test_timestamp_parsing(spark, period, want):
    assert reason_for(spark, [(period, "D", "95000", "t")])[(period, "D")] == want


def test_unknown_type_is_quarantined(spark):
    assert reason_for(spark, [("2020-01-01T00", "XX", "95000", "t")]) \
        == {("2020-01-01T00", "XX"): "UNKNOWN_TYPE"}


def test_quarantine_rows_carry_a_recoverable_reason(spark):
    _, q = split_quarantine(classify(resolve_revisions(bronze(spark, [
        ("2021-10-19T04", "D", "2147480000", "t"),
    ]))))
    r = q.first()
    assert r["reason"] == "IMPOSSIBLE_VALUE"
    assert r["raw_value"] == "2147480000"     # original string preserved
    assert r["region_id"] == "PJM" and r["metric_type"] == "D"


# --------------------------------------------------------------------- pivot

def test_four_types_become_one_row(spark):
    valid, _ = split_quarantine(classify(resolve_revisions(bronze(spark, [
        ("2026-08-01T00", "D", "127017", "t"),
        ("2026-08-01T00", "DF", "123686", "t"),
        ("2026-08-01T00", "NG", "129042", "t"),
        ("2026-08-01T00", "TI", "542", "t"),
    ]))))
    w = pivot_to_wide(valid)
    assert w.count() == 1
    r = w.first()
    assert (r["actual_demand_mwh"], r["day_ahead_forecast_mwh"]) == (127017.0, 123686.0)
    assert (r["net_generation_mwh"], r["interchange_mwh"]) == (129042.0, 542.0)


def test_quarantined_metric_leaves_a_null_not_a_missing_row(spark):
    """DF stays usable even when D for the same hour is unusable."""
    valid, _ = split_quarantine(classify(resolve_revisions(bronze(spark, [
        ("2021-10-19T04", "D", "2147480000", "t"),
        ("2021-10-19T04", "DF", "84000", "t"),
    ]))))
    r = pivot_to_wide(valid).first()
    assert r["actual_demand_mwh"] is None
    assert r["day_ahead_forecast_mwh"] == 84000.0


# --------------------------------------------------------------------- spine

def test_missing_hour_becomes_an_explicit_row(spark):
    valid, _ = split_quarantine(classify(resolve_revisions(bronze(spark, [
        ("2020-01-01T00", "D", "95000", "t"),
        ("2020-01-01T02", "D", "93000", "t"),      # 01:00 absent upstream
    ]))))
    s = attach_hour_spine(pivot_to_wide(valid), spark).orderBy("event_timestamp_utc")
    rows = s.collect()
    assert len(rows) == 3
    assert [r["is_missing_hour"] for r in rows] == [False, True, False]
    assert rows[1]["actual_demand_mwh"] is None


# ---------------------------------------------------------------- local time

def test_fall_back_keeps_two_distinct_rows_for_one_local_hour(spark):
    """05:00 and 06:00 UTC are both 01:00 local on 2023-11-05. Keying on local time
    would collapse two real hours into one."""
    valid, _ = split_quarantine(classify(resolve_revisions(bronze(spark, [
        ("2023-11-05T05", "D", "70000", "t"),
        ("2023-11-05T06", "D", "69000", "t"),
    ]))))
    df = add_local_time(pivot_to_wide(valid)).orderBy("event_timestamp_utc")
    rows = df.collect()
    assert [r["local_hour"] for r in rows] == [1, 1]
    assert rows[0]["event_timestamp_utc"] != rows[1]["event_timestamp_utc"]
    assert len({r["actual_demand_mwh"] for r in rows}) == 2


def test_spring_forward_has_no_local_two_am(spark):
    periods = [(f"2026-03-08T{h:02d}", "D", "80000", "t") for h in range(24)]
    valid, _ = split_quarantine(classify(resolve_revisions(bronze(spark, periods))))
    hours = {r["local_hour"] for r in add_local_time(pivot_to_wide(valid)).collect()}
    assert 2 not in hours


# ------------------------------------------------------------------- anomaly

def _series(spark, values):
    rows = [(f"2020-07-{1 + i // 24:02d}T{i % 24:02d}", "D", str(v), "t")
            for i, v in enumerate(values)]
    valid, _ = split_quarantine(classify(resolve_revisions(bronze(spark, rows))))
    df = flag_anomalies(attach_hour_spine(pivot_to_wide(valid), spark))
    return df.orderBy("event_timestamp_utc").collect()


# 262,651 is the real 2020-07-24T16 reading, with ~118,000 either side. It is the
# hardest class to catch: a plausible magnitude that the hard bound must not reject,
# so layer 2 has to earn it. 417,669 is NOT used here - see the layer-boundary test.
PLAUSIBLE_SPIKE = 262_651


def test_isolated_spike_is_flagged_but_kept(spark):
    vals = [118_000] * 44 + [PLAUSIBLE_SPIKE] + [118_000] * 27
    rows = _series(spark, vals)
    spike = [r for r in rows if r["actual_demand_mwh"] == float(PLAUSIBLE_SPIKE)]
    assert len(spike) == 1
    assert spike[0]["is_demand_anomaly_suspect"] is True
    assert len(rows) == len(vals)            # flagged, not removed


def test_neighbours_of_a_spike_are_not_flagged(spark):
    """Rejects the interpolation detector, which flagged three rows per fault."""
    vals = [118_000] * 44 + [PLAUSIBLE_SPIKE] + [118_000] * 27
    rows = _series(spark, vals)
    flagged = [r for r in rows if r["is_demand_anomaly_suspect"]]
    assert [r["actual_demand_mwh"] for r in flagged] == [float(PLAUSIBLE_SPIKE)]


def test_layer_boundary_hard_bound_takes_417k_before_the_flag_sees_it(spark):
    """Makes the division of labour a tested contract rather than an accident.

    417,669 (real, 2019-12-11T20) exceeds the hard bound, so it is quarantined and
    never reaches the anomaly flag. 262,651 sits inside the bound and must survive to
    be flagged. Both are genuine faults; they are caught by different layers.
    """
    reasons = reason_for(spark, [
        ("2019-12-11T20", "D", "417669", "t"),
        ("2020-07-24T16", "D", "262651", "t"),
    ])
    assert reasons[("2019-12-11T20", "D")] == "IMPOSSIBLE_VALUE"
    assert reasons[("2020-07-24T16", "D")] is None


def test_steep_daily_ramp_is_not_flagged(spark):
    """Rejects the rolling-median detector, which flagged a valid 138,575 at 13:00."""
    day = [95_000, 92_000, 90_000, 89_000, 88_000, 90_000, 95_000, 100_000,
           104_000, 108_000, 112_000, 116_000, 120_000, 124_000, 128_000, 132_000,
           136_000, 138_000, 137_000, 133_000, 128_000, 120_000, 110_000, 100_000]
    rows = _series(spark, day * 3)
    assert not any(r["is_demand_anomaly_suspect"] for r in rows)


def test_spine_preserves_utc_and_does_not_shift_by_driver_timezone(spark):
    """Regression: building the range via collect() + strftime shifted every timestamp
    by the driver's UTC offset (+7 here), so the join silently matched nothing."""
    valid, _ = split_quarantine(classify(resolve_revisions(bronze(spark, [
        ("2020-01-01T00", "D", "95000", "t"),
        ("2020-01-01T02", "D", "93000", "t"),
    ]))))
    s = attach_hour_spine(pivot_to_wide(valid), spark)
    # Formatted inside Spark, under the session's UTC zone. Calling strftime on a
    # collected datetime would re-introduce the driver-offset bug into the assertion.
    got = {r["hh"]: r["actual_demand_mwh"] for r in
           s.withColumn("hh", F.date_format("event_timestamp_utc", "yyyy-MM-dd'T'HH"))
            .collect()}
    assert got == {"2020-01-01T00": 95000.0,
                   "2020-01-01T01": None,
                   "2020-01-01T02": 93000.0}


def test_day_ahead_forecast_is_realigned_onto_the_demand_timeline(spark):
    """EIA labels DF one hour earlier than D for the same physical hour. Measured over
    67,096 paired hours: DF(t) matches D(t+1) at MAE 2,224.8 versus 3,320.5 at D(t).
    Benchmarking without this correction inflates hourly MAE by 49% and reports the
    benchmark's peak-timing hit rate as 13.7% rather than 62.5%.
    """
    rows = []
    for h in range(4):
        rows.append((f"2024-06-01T{h:02d}", "D", str(100_000 + h * 1000), "t"))
        rows.append((f"2024-06-01T{h:02d}", "DF", str(100_000 + (h + 1) * 1000), "t"))
    silver, _ = build_silver(bronze(spark, rows), spark)
    got = {r["hh"]: (r["actual_demand_mwh"],
                     r["day_ahead_forecast_mwh"],
                     r["day_ahead_forecast_as_published_mwh"])
           for r in silver.withColumn(
               "hh", F.date_format("event_timestamp_utc", "HH")).collect()}
    # As published, DF at hour h forecasts hour h+1, so after alignment the forecast
    # sitting on hour h must equal what was published against hour h-1.
    assert got["01"] == (101_000.0, 101_000.0, 102_000.0)
    assert got["02"] == (102_000.0, 102_000.0, 103_000.0)
    # The first hour has no predecessor to draw from.
    assert got["00"][1] is None


def test_alignment_needs_the_spine_to_be_gap_free(spark):
    """`lag` counts rows, not hours. The spine is what makes one row equal one hour."""
    rows = [("2024-06-01T00", "D", "100000", "t"), ("2024-06-01T00", "DF", "101000", "t"),
            ("2024-06-01T03", "D", "103000", "t"), ("2024-06-01T03", "DF", "104000", "t")]
    silver, _ = build_silver(bronze(spark, rows), spark)
    rows_out = silver.orderBy("event_timestamp_utc").collect()
    assert len(rows_out) == 4                      # 00,01,02,03 - gaps materialised
    assert rows_out[3]["day_ahead_forecast_mwh"] is None   # predecessor hour is empty

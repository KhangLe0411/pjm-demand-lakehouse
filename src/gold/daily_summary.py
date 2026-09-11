"""Gold: one row per region per local operating day.

The operating day is *local*, not UTC, because that is the day a balancing authority
plans against and the day a peak belongs to. It is therefore 23, 24 or 25 hours long
and its length is joined in from the calendar rather than assumed.

Peak statistics deliberately exclude rows carrying `is_demand_anomaly_suspect`. This is
the whole reason Silver flags rather than drops: a 262,651 MWh reading passes the hard
bound, so without the flag it would be published as PJM's all-time peak and would
poison every peak metric downstream.
"""
from __future__ import annotations

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F


def daily_summary(silver: DataFrame, calendar: DataFrame,
                  exclude_suspect: bool = True) -> DataFrame:
    """`exclude_suspect=False` exists so the cost of the filter can be measured, not
    because running without it is a supported mode."""
    usable = F.col("actual_demand_mwh").isNotNull()
    if exclude_suspect:
        # Both flags, because they catch different classes: the relative detector finds
        # isolated spikes, the absolute ceiling finds readings whose neighbours are
        # inflated too. 176,085 MWh on 2020-07-29 escaped the first and not the second.
        usable = usable & ~F.col("is_demand_anomaly_suspect") \
            & ~F.col("is_above_historical_record")

    demand = F.when(usable, F.col("actual_demand_mwh"))
    forecast = F.when(F.col("day_ahead_forecast_mwh").isNotNull(),
                      F.col("day_ahead_forecast_mwh"))

    agg = (
        silver.groupBy("region_id", "local_date").agg(
            F.count("*").alias("actual_hours"),
            F.sum(usable.cast("int")).alias("demand_hours"),
            F.sum(F.col("is_demand_anomaly_suspect").cast("int")).alias("suspect_hours"),
            F.sum(F.col("is_above_historical_record").cast("int")).alias("above_record_hours"),
            F.sum(F.col("is_missing_hour").cast("int")).alias("missing_hours"),
            F.max(demand).alias("peak_demand_mwh"),
            F.min(demand).alias("min_demand_mwh"),
            F.avg(demand).alias("mean_demand_mwh"),
            F.max(forecast).alias("day_ahead_peak_mwh"),
            # argmax by struct ordering: the max struct carries its own hour, which
            # avoids a second pass or a self-join
            F.max(F.when(usable, F.struct("actual_demand_mwh", "local_hour",
                                          "event_timestamp_utc"))).alias("_pk"),
            F.max(F.when(F.col("day_ahead_forecast_mwh").isNotNull(),
                         F.struct("day_ahead_forecast_mwh", "local_hour"))).alias("_pkf"),
        )
    )
    return (
        agg.join(calendar, "local_date", "left")
        .withColumn("peak_local_hour", F.col("_pk.local_hour"))
        .withColumn("peak_timestamp_utc", F.col("_pk.event_timestamp_utc"))
        .withColumn("day_ahead_peak_local_hour", F.col("_pkf.local_hour"))
        .withColumn("load_factor",
                    F.round(F.col("mean_demand_mwh") / F.col("peak_demand_mwh"), 4))
        # Signed, so a persistent bias is visible rather than averaged away by abs().
        .withColumn("peak_magnitude_error_mwh",
                    F.col("day_ahead_peak_mwh") - F.col("peak_demand_mwh"))
        .withColumn("peak_magnitude_error_pct",
                    F.round(100 * (F.col("day_ahead_peak_mwh") - F.col("peak_demand_mwh"))
                            / F.col("peak_demand_mwh"), 3))
        .withColumn("peak_timing_error_hours",
                    F.col("day_ahead_peak_local_hour") - F.col("peak_local_hour"))
        .withColumn("is_complete",
                    (F.col("actual_hours") == F.col("expected_hours"))
                    & (F.col("demand_hours") == F.col("expected_hours")))
        .drop("_pk", "_pkf")
    )


def peak_history(daily: DataFrame) -> DataFrame:
    """Running all-time peak, over complete days only.

    Incomplete days are excluded because a day holding two of its hours cannot
    establish a record, and one bad reading on such a day would otherwise stand as the
    all-time maximum forever.
    """
    w = (Window.partitionBy("region_id").orderBy("local_date")
         .rowsBetween(Window.unboundedPreceding, Window.currentRow))
    return (
        daily.filter("is_complete")
        .withColumn("running_peak_mwh", F.max("peak_demand_mwh").over(w))
        .withColumn("is_new_record", F.col("peak_demand_mwh") == F.col("running_peak_mwh"))
        .select("region_id", "local_date", "peak_demand_mwh", "peak_local_hour",
                "running_peak_mwh", "is_new_record")
    )

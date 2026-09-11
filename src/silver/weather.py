"""Bronze -> Silver for archived weather forecasts.

Grain: one row per (valid hour, point, forecast vintage), matching README section 15.
`forecast_lead_days` replaces that section's `forecast_run_timestamp_utc`: the archive
exposes "N days earlier", not a run instant, and inventing a timestamp would make the
leakage audit assert something that was never measured (docs/decisions.md D-21).
"""
from __future__ import annotations

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

# 65 degrees F, the standard degree-day base in load forecasting, expressed in Celsius
# because that is the unit the source returns.
DEGREE_DAY_BASE_C = 18.333333

# Physical bounds for the PJM footprint, wide on purpose: these quarantine corruption,
# not unusual weather. US record extremes are roughly -57 C and +57 C.
BOUNDS = {
    "temperature_c":          (-60.0, 60.0),
    "apparent_temperature_c": (-75.0, 75.0),   # wind chill / heat index widen the range
    "dew_point_c":            (-60.0, 45.0),
    "relative_humidity_pct":  (0.0, 100.0),
    "wind_speed_kmh":         (0.0, 500.0),
    "precipitation_mm":       (0.0, 500.0),
}

KEY = ["valid_timestamp_utc", "point_id", "forecast_lead_days"]


def resolve_revisions(landing: DataFrame) -> DataFrame:
    w = Window.partitionBy(*KEY).orderBy(F.col("ingested_at").desc())
    return (landing.withColumn("_rn", F.row_number().over(w))
            .filter(F.col("_rn") == 1).drop("_rn"))


def classify(resolved: DataFrame) -> DataFrame:
    """Bounds are applied per column, and a violation nulls only that column.

    Nulling the single offending measure rather than dropping the row matters here:
    humidity, wind and precipitation are absent before 2024-01-19 by design, so a
    row-level rule would discard every temperature reading from the longer history
    that the primary model depends on.
    """
    ts = F.to_timestamp("valid_timestamp_utc")
    # Replace the string in place rather than adding a second column beside it: two
    # columns for one concept is how a downstream join silently picks the wrong one.
    out = resolved.withColumn("valid_timestamp_utc", ts)

    bad = {col: F.col(col).isNotNull() & ((F.col(col) < lo) | (F.col(col) > hi))
           for col, (lo, hi) in BOUNDS.items()}

    # The record of what failed must be materialised BEFORE the offending columns are
    # nulled. A Spark Column is a lazy expression resolved against the plan where it is
    # *used*, not where it is built, so nulling first and recording afterwards makes
    # every `bad` test read the already-nulled value and report nothing.
    out = out.withColumn(
        "out_of_bounds_columns",
        F.array_compact(F.array(*[F.when(b, F.lit(c)) for c, b in bad.items()])))
    for col, b in bad.items():
        out = out.withColumn(col, F.when(b, None).otherwise(F.col(col)))

    return out.withColumn("reason", F.when(ts.isNull(), F.lit("INVALID_TIMESTAMP")))


def add_degree_days(df: DataFrame) -> DataFrame:
    """Hourly cooling/heating degrees.

    Load responds to temperature in a U shape - both hot and cold raise demand - so a
    raw temperature term forces any linear model to pick one side. Splitting into two
    non-negative terms encodes the shape directly.
    """
    t = F.col("temperature_c")
    # The `when(t.isNotNull(), ...)` guard is load-bearing. `F.greatest` SKIPS nulls, so
    # `greatest(NULL - base, 0.0)` returns 0.0, not NULL: a missing temperature would
    # silently become "zero cooling degrees", i.e. "it was mild" - a fabricated value
    # the model would train on. Absence has to stay absent.
    return (df
            .withColumn("cdd_c", F.when(t.isNotNull(),
                                        F.greatest(t - F.lit(DEGREE_DAY_BASE_C),
                                                   F.lit(0.0))))
            .withColumn("hdd_c", F.when(t.isNotNull(),
                                        F.greatest(F.lit(DEGREE_DAY_BASE_C) - t,
                                                   F.lit(0.0)))))


def build_weather_silver(landing: DataFrame) -> tuple[DataFrame, DataFrame]:
    classified = classify(resolve_revisions(landing))
    valid = add_degree_days(classified.filter(F.col("reason").isNull()))
    quarantine = (classified.filter(F.col("reason").isNotNull())
                  .select(*KEY, "source_file", "reason",
                          F.current_timestamp().alias("detected_at")))
    keep = ["region_id", "valid_timestamp_utc", "point_id", "forecast_lead_days",
            "latitude", "longitude", *BOUNDS, "cdd_c", "hdd_c",
            "out_of_bounds_columns", "ingested_at"]
    return valid.select(*keep), quarantine

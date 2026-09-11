"""Bronze -> Silver for EIA-930 electricity data.

Grain of the output: one row per (region, event hour) on a gap-free UTC spine
(README section 14). Bronze arrives long and string-typed with one row per
(period, type, ingestion event); Silver resolves vintages, validates, and pivots.

Order matters, and this is the order:

  1. resolve revisions   latest vintage per (period, respondent, type)
  2. validate            structural + numeric + hard bounds  -> quarantine
  3. pivot               type -> column
  4. spine               left-join a complete hour range, so gaps become rows
  5. local time          derived from the canonical UTC key
  5b. align DF           the published forecast is labelled one hour early
  6. anomaly flag        non-destructive; row stays in Silver

Revisions are resolved *before* validation on purpose. If a bad value was later
corrected upstream, only the surviving vintage is judged, so quarantine reports "the
current best value is bad" rather than "some superseded vintage was bad".
"""
from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

PJM_TZ = "America/New_York"

TYPE_MAP = {
    "D":  "actual_demand_mwh",
    "DF": "day_ahead_forecast_mwh",
    "NG": "net_generation_mwh",
    "TI": "interchange_mwh",
}

# Layer 1 of two. Deliberately generous: these must never reject a genuine reading,
# only a physically impossible one. PJM's all-time peak is ~165,563 MW, so 300,000 is
# roughly 1.8x a record that has stood since 2006. Calibrated against observed history
# by scripts/validate_sources.py; see docs/decisions.md D-18.
HARD_BOUNDS = {
    "D":  (10_000.0, 300_000.0),
    "DF": (10_000.0, 300_000.0),
    "NG": (0.0, 300_000.0),
    "TI": (-100_000.0, 100_000.0),   # interchange is signed - a net exporter is negative
}

# Layer 2 threshold. Flags only; never removes a row.
ANOMALY_RATIO = 1.30

# Layer 3. PJM's all-time peak, ~165,563 MW in August 2006 (pjm.com). A *relative*
# detector compares each hour with its neighbours, so a reading whose neighbours are
# also inflated slips through: 176,085 MWh on 2020-07-29 scored only 1.26. An absolute
# ceiling catches exactly that class. It flags rather than rejects, so a genuine future
# record surfaces for review instead of being discarded - 2026 peaks already reach
# 162,648, so this will begin firing on real records, and that is the intended behaviour.
PJM_RECORD_PEAK_MWH = 165_563.0

QUARANTINE_COLUMNS = [
    "source_file", "period", "region_id", "metric_type", "raw_value",
    "reason", "detected_at",
]


# ------------------------------------------------------------------ 1. revisions

def resolve_revisions(bronze: DataFrame) -> DataFrame:
    """Keep the newest vintage per logical key and record how many there were.

    `revision_count` counts *distinct published values*, not ingestion events, so a
    re-fetch that returned identical data is not mistaken for an upstream revision
    (README section 13).
    """
    key = ["period", "respondent", "type"]
    w = Window.partitionBy(*key).orderBy(F.col("ingested_at").desc())
    counts = Window.partitionBy(*key)
    return (
        bronze
        .withColumn("_rn", F.row_number().over(w))
        .withColumn("vintage_count", F.count("*").over(counts))
        .withColumn("revision_count",
                    F.size(F.collect_set("value").over(counts)) - 1)
        .filter(F.col("_rn") == 1)
        .drop("_rn")
    )


# ----------------------------------------------------------------- 2. validation

def _bounds_expr(value_col: str, type_col: str):
    """Per-type bound test. A single blanket `value >= 0` would quarantine every
    negative interchange reading, of which the real data holds 1,627 valid ones."""
    expr = F.lit(False)
    for t, (lo, hi) in HARD_BOUNDS.items():
        expr = F.when(F.col(type_col) == t,
                      (F.col(value_col) < lo) | (F.col(value_col) > hi)).otherwise(expr)
    return expr


def classify(resolved: DataFrame) -> DataFrame:
    """Attach a `reason` to every row that fails a contract; NULL means it passed."""
    known = F.col("type").isin(list(TYPE_MAP))
    ts = F.to_timestamp("period", "yyyy-MM-dd'T'HH")
    numeric = F.col("value").cast("double")

    return (
        resolved
        .withColumn("event_timestamp_utc", ts)
        .withColumn("value_mwh", numeric)
        .withColumn("reason", F.coalesce(
            # Order is the precedence order: structure, then type, then numerics.
            F.when(F.col("period").isNull() | F.col("respondent").isNull()
                   | F.col("type").isNull(), F.lit("MISSING_REQUIRED_FIELD")),
            F.when(ts.isNull(), F.lit("INVALID_TIMESTAMP")),
            F.when(~known, F.lit("UNKNOWN_TYPE")),
            # A NULL upstream value is a *reported* absence, not a defect - it stays and
            # becomes a NULL metric. Only a non-null string that will not cast is a fault.
            F.when(F.col("value").isNotNull() & numeric.isNull(),
                   F.lit("INVALID_NUMERIC")),
            F.when(numeric.isNotNull() & _bounds_expr("value_mwh", "type"),
                   F.lit("IMPOSSIBLE_VALUE")),
        ))
    )


def split_quarantine(classified: DataFrame) -> tuple[DataFrame, DataFrame]:
    valid = classified.filter(F.col("reason").isNull()).drop("reason")
    quarantine = (
        classified.filter(F.col("reason").isNotNull())
        .withColumn("region_id", F.col("respondent"))
        .withColumn("metric_type", F.col("type"))
        .withColumn("raw_value", F.col("value"))
        .withColumn("detected_at", F.current_timestamp())
        .select(*QUARANTINE_COLUMNS)
    )
    return valid, quarantine


# ---------------------------------------------------------------------- 3. pivot

def pivot_to_wide(valid: DataFrame) -> DataFrame:
    aggs = [
        F.max(F.when(F.col("type") == t, F.col("value_mwh"))).alias(col)
        for t, col in TYPE_MAP.items()
    ]
    return (
        valid.groupBy(F.col("respondent").alias("region_id"), "event_timestamp_utc")
        .agg(*aggs,
             F.max("ingested_at").alias("ingested_at"),
             F.max("revision_count").alias("revision_count"))
    )


# ---------------------------------------------------------------------- 4. spine

def attach_hour_spine(wide: DataFrame, spark: SparkSession) -> DataFrame:
    """Left-join onto a gap-free hourly range so absent hours become explicit rows.

    Silently dropping them would hide the gap from the completeness check and would
    also break the +/-24h anomaly comparison, which assumes contiguity.

    The range is built entirely inside Spark, deliberately. Collecting the min/max to
    the driver and formatting them back into SQL looks equivalent and is not:
    `collect()` hands back a *naive* Python datetime converted to the driver's local
    zone, so on a UTC+7 machine `2020-01-01T00:00Z` returns as `07:00`, is re-parsed as
    `07:00Z` under a UTC session, and the join then matches nothing at all — with no
    error raised anywhere. Timestamps must not round-trip through the driver.
    """
    metrics = list(TYPE_MAP.values())
    spine = (
        wide.agg(F.min("event_timestamp_utc").alias("lo"),
                 F.max("event_timestamp_utc").alias("hi"))
        .select(F.explode(F.expr("sequence(lo, hi, interval 1 hour)"))
                 .alias("event_timestamp_utc"))
        .crossJoin(wide.select("region_id").distinct())
    )
    return (
        spine.join(wide, ["region_id", "event_timestamp_utc"], "left")
        .withColumn("is_missing_hour",
                    F.greatest(*[F.col(m).isNotNull().cast("int") for m in metrics]) == 0)
    )


# ----------------------------------------------------------------- 5. local time

def add_local_time(df: DataFrame, tz: str = PJM_TZ) -> DataFrame:
    """UTC stays the key; local time is derived. Never the other way round.

    On a fall-back day two distinct UTC instants map to the same local wall clock, so
    keying on local time would collapse two real hours into one row.
    """
    local = F.from_utc_timestamp("event_timestamp_utc", tz)
    return (
        df.withColumn("local_timestamp", local)
        .withColumn("local_date", F.to_date(local))
        .withColumn("local_hour", F.hour(local))
        .withColumn("timezone", F.lit(tz))
    )


# ------------------------------------------------------- 5b. benchmark alignment

def align_day_ahead_forecast(df: DataFrame) -> DataFrame:
    """Put `DF` on the same hour timeline as `D`.

    Measured over 67,096 paired hours, the published forecast matches demand one hour
    LATER far better than the hour it is labelled with:

        DF(t) vs D(t-1)   MAE 5,247.5   corr 0.92199
        DF(t) vs D(t+0)   MAE 3,320.5   corr 0.96973
        DF(t) vs D(t+1)   MAE 2,224.8   corr 0.98732   <- best

    No physical mechanism makes a *forecast* better at the hour after the one it aims
    at, so the two series carry different hour-labelling conventions rather than the
    forecast being weak. Corroborated from outside the data: aligned hourly MAPE is
    2.435%, inside the 1.5-3% range published day-ahead forecasts for large balancing
    authorities are known to achieve, while the misaligned 3.608% sits outside it.

    Left uncorrected this is not a small error. It inflates hourly MAE by 49% and
    understates the benchmark's peak-timing hit rate as 13.7% instead of 62.5% —
    a challenger could then "beat" the benchmark by shifting an hour and have beaten
    an artefact (docs/decisions.md D-28).

    The demand timeline is kept as canonical because demand is the label. This
    establishes the *relative* offset only; which series is absolutely right is a
    separate question, recorded as open.

    Requires the gap-free spine: `lag` counts rows, so a missing hour would silently
    shift the alignment by more than an hour.
    """
    order = Window.partitionBy("region_id").orderBy("event_timestamp_utc")
    return (
        df.withColumnRenamed("day_ahead_forecast_mwh",
                             "day_ahead_forecast_as_published_mwh")
        .withColumn("day_ahead_forecast_mwh",
                    F.lag("day_ahead_forecast_as_published_mwh", 1).over(order))
    )


# ---------------------------------------------------------------- 6. anomaly flag

def flag_anomalies(df: DataFrame, ratio: float = ANOMALY_RATIO) -> DataFrame:
    """Compare each hour with the SAME CLOCK HOUR on the days either side.

    Both neighbours must disagree, independently, and both must exist. That is what
    removes contagion: a spike corrupts only one of its two comparisons, so the other
    vetoes the flag. `greatest`/`least` cannot express this — they skip NULLs, which
    lets a lone present neighbour decide by itself and re-introduces the very problem
    this detector exists to avoid.

    Three detectors were tried against the real backfill before this one:
      * deviation from interpolated neighbours — one bad value poisons both adjacent
        hours, flagging three rows per genuine fault;
      * ratio to a centred rolling median — fires on steep ramp hours, e.g. a valid
        138,575 MWh at 13:00 scored 1.34;
      * same-hour comparison via greatest/least — flags the spike's own +/-24h
        neighbours as low anomalies, for the NULL reason above.
    """
    order = Window.partitionBy("region_id").orderBy("event_timestamp_utc")
    prev = F.lag("actual_demand_mwh", 24).over(order)
    nxt = F.lead("actual_demand_mwh", 24).over(order)
    d = F.col("actual_demand_mwh")

    comparable = d.isNotNull() & prev.isNotNull() & nxt.isNotNull()
    too_high = (d > F.lit(ratio) * prev) & (d > F.lit(ratio) * nxt)
    too_low = (d * F.lit(ratio) < prev) & (d * F.lit(ratio) < nxt)

    return (
        df.withColumn("is_demand_anomaly_suspect",
                      F.when(comparable, too_high | too_low).otherwise(F.lit(False)))
        .withColumn("is_above_historical_record",
                    F.coalesce(d > F.lit(PJM_RECORD_PEAK_MWH), F.lit(False)))
    )


# ------------------------------------------------------------------- orchestration

def build_silver(bronze: DataFrame, spark: SparkSession,
                 tz: str = PJM_TZ) -> tuple[DataFrame, DataFrame]:
    resolved = resolve_revisions(bronze)
    valid, quarantine = split_quarantine(classify(resolved))
    silver = flag_anomalies(align_day_ahead_forecast(add_local_time(
        attach_hour_spine(pivot_to_wide(valid), spark), tz)))
    # Explicit order: keys, then measures, then the derived local view, then flags,
    # then lineage. A published table is read by people, not only by joins.
    return silver.select(
        "region_id", "event_timestamp_utc",
        "local_timestamp", "local_date", "local_hour", "timezone",
        "actual_demand_mwh",
        "day_ahead_forecast_mwh", "day_ahead_forecast_as_published_mwh",
        "net_generation_mwh", "interchange_mwh",
        "is_missing_hour", "is_demand_anomaly_suspect", "is_above_historical_record",
        "revision_count", "ingested_at",
    ), quarantine


def completeness(silver: DataFrame) -> DataFrame:
    """Rows per local operating day against the calendar's own expectation.

    `expected_hours` is joined in by the caller from
    `src.silver.calendar_utils.expected_hours`; hard-coding 24 here would report a
    false gap on every spring-forward and a false surplus on every fall-back.
    """
    return (
        silver.groupBy("region_id", "local_date")
        .agg(F.count("*").alias("actual_hours"),
             F.sum(F.col("actual_demand_mwh").isNotNull().cast("int")).alias("demand_hours"),
             F.sum(F.col("is_missing_hour").cast("int")).alias("missing_hours"),
             F.sum(F.col("is_demand_anomaly_suspect").cast("int")).alias("suspect_hours"))
    )

# Data dictionary

Every column in every layer, with where it comes from and why it exists.

Provenance tags: **[src]** verbatim from the source API · **[ing]** added by the
extractor · **[al]** added by Auto Loader / the Bronze load · **[der]** derived in Silver
or Gold.

---

## Layer 0 — Raw: what the EIA API actually returns

The route publishes **one** measure column and uses a **long** shape, which is why a raw
record looks narrow. `type` is the discriminator, so a single hour arrives as four rows
rather than one row with four measures. Width appears at Silver, via the pivot.

Route metadata (`GET /v2/electricity/rto/region-data/`):

| Field | Value |
|---|---|
| `data` columns | `value` only — units `megawatthours`, aggregation `SUM` |
| `facets` | `respondent` (balancing authority), `type` (metric) |
| `frequency` | `hourly` (UTC) or `local-hourly` (local, offset in the period string) |
| `startPeriod` | `2019-01-01T00` — the floor behind D-02 |
| `defaultDateFormat` | `YYYY-MM-DD"T"HH24` |

### The response envelope

| Field | Meaning |
|---|---|
| `request.command` | the route that was called |
| `request.params` | **echoes the api_key back** — never log a full response |
| `apiVersion` | e.g. `2.1.13` |
| `response.total` | total matching records, *ignoring* `length` — this is what pagination follows |
| `response.dateFormat` | the format `period` is written in |
| `response.frequency` | the frequency actually served |
| `response.description` | prose description of the dataset |
| `response.data` | the array of records |

### A record inside `response.data` — all 7 fields

| Field | Type | Meaning |
|---|---|---|
| `period` | string | Hour. `hourly` gives `2026-07-02T22`, no offset, **UTC** (established by measurement, D-13). `local-hourly` gives `2026-07-02T18-04`, offset included. |
| `respondent` | string | Balancing authority code, `PJM`. 83 exist. |
| `respondent-name` | string | `PJM Interconnection, LLC` |
| `type` | string | `D` demand · `DF` day-ahead demand forecast · `NG` net generation · `TI` total interchange |
| `type-name` | string | Human label for `type` |
| `value` | **string** | The measure. Quoted in the JSON, `"162648"`, and negative for `TI`: `"-8"` |
| `value-units` | string | `megawatthours` |

Note the hyphens. `respondent-name`, `type-name` and `value-units` are not valid
identifiers, so the extractor maps them to underscores.

### Raw weather (Open-Meteo Previous Runs)

The envelope carries location metadata; the payload is column-oriented arrays, not
records.

| Field | Meaning |
|---|---|
| `latitude` / `longitude` | the grid point actually used — **snapped**, not what you asked for. Requesting 39.95/-75.17 returns 39.935707/-75.16384. |
| `elevation` | metres, of the snapped point |
| `utc_offset_seconds`, `timezone`, `timezone_abbreviation` | `0` / `GMT` / `GMT` because every request pins `timezone=UTC` (D-03) |
| `generationtime_ms` | server-side timing, diagnostic only |
| `hourly_units` | unit per variable, e.g. `°C`, `%`, `km/h`, `mm` |
| `hourly.time` | array of ISO timestamps |
| `hourly.<var>_previous_dayN` | array parallel to `time`; `N` is the forecast vintage |

Multiple coordinates return a JSON **array** of these objects, one per point, in request
order — that ordering is what maps a response back to a `point_id`.

---

## Layer 1 — `energy.bronze.eia_region_data`

Append-only. Raw is preserved, including `value` as a string, so that a malformed value
lands and is quarantined downstream instead of failing ingestion.

| # | Column | Type | Src | Meaning |
|---|---|---|---|---|
| 1 | `period` | string | [src] | Hour as published, UTC |
| 2 | `respondent` | string | [src] | BA code |
| 3 | `respondent_name` | string | [src] | de-hyphenated from `respondent-name` |
| 4 | `type` | string | [src] | `D` / `DF` / `NG` / `TI` |
| 5 | `type_name` | string | [src] | de-hyphenated |
| 6 | `value` | **string** | [src] | **Deliberately not cast.** Casting at ingest would make a bad value crash the load; as a string it reaches quarantine with its original text intact. |
| 7 | `value_units` | string | [src] | `megawatthours` |
| 8 | `source` | string | [ing] | Route identifier, so a row can be traced to the endpoint that produced it |
| 9 | `source_file` | string | [ing] | Landing file the extractor wrote |
| 10 | `ingested_at` | string | [ing] | Extractor run instant. **The vintage key** — Silver resolves revisions by ordering on this. |
| 11 | `date` | date | [ing] | Hive partition of the landing path |
| 12 | `_source_file` | string | [al] | File Auto Loader read, from `_metadata.file_path` |
| 13 | `_file_modified` | timestamp | [al] | From `_metadata.file_modification_time` |
| 14 | `_bronze_loaded_at` | timestamp | [al] | When Bronze ingested it. Distinct from `ingested_at`: one is when the API was called, the other when Databricks loaded the file. |

Grain: **one source record × one ingestion event.** A re-fetch of an already-loaded hour
adds a row rather than replacing one, which is what makes revisions measurable.

---

## Layer 2 — `energy.silver.electricity_hourly`

Grain: **one region × one event hour**, on a gap-free UTC spine.

| # | Column | Type | Src | Meaning |
|---|---|---|---|---|
| 1 | `region_id` | string | [der] | From `respondent` |
| 2 | `event_timestamp_utc` | timestamp | [der] | **The canonical key.** UTC because local time repeats an hour each autumn and skips one each spring; keying on local would collapse two real hours into one row. |
| 3 | `local_timestamp` | timestamp | [der] | Derived view, `America/New_York` |
| 4 | `local_date` | date | [der] | Local operating date — the day a peak belongs to |
| 5 | `local_hour` | int | [der] | 0-23 local |
| 6 | `timezone` | string | [der] | The zone used, recorded rather than assumed |
| 7 | `actual_demand_mwh` | double | [der] | `type=D`, cast |
| 8 | `day_ahead_forecast_mwh` | double | [der] | `type=DF`, cast **and shifted onto the demand timeline**. Use this one for benchmarking (D-28). |
| 9 | `day_ahead_forecast_as_published_mwh` | double | [der] | Same value at the period EIA labelled it with. Kept only so the correction is auditable. |
| 10 | `net_generation_mwh` | double | [der] | `type=NG` |
| 11 | `interchange_mwh` | double | [der] | `type=TI`. **Signed** — negative means net export. 1,627 real rows are negative, which is why bounds are per type. |
| 12 | `is_missing_hour` | boolean | [der] | The hour exists on the spine but no metric arrived. Explicit, rather than an absent row. |
| 13 | `is_demand_anomaly_suspect` | boolean | [der] | DQ layer 2: disagrees with the same clock hour on both adjacent days |
| 14 | `is_above_historical_record` | boolean | [der] | DQ layer 3: above PJM's 2006 peak of 165,563 MW |
| 15 | `revision_count` | int | [der] | Distinct *published values* seen for this key, minus one. Counts revisions, not re-fetches. |
| 16 | `ingested_at` | string | [der] | Vintage that survived resolution |

Both flags flag rather than remove. A quarantine decision is irreversible; a flag lets
the model and the Gold layer each decide.

## `energy.silver.electricity_quarantine`

| Column | Type | Meaning |
|---|---|---|
| `source_file` | string | Landing file, so the row can be traced and reprocessed |
| `period` | string | Hour as published |
| `region_id` | string | BA |
| `metric_type` | string | Which of D/DF/NG/TI failed |
| `raw_value` | string | **Original text**, uncast — the point of quarantine |
| `reason` | string | `MISSING_REQUIRED_FIELD` · `INVALID_TIMESTAMP` · `UNKNOWN_TYPE` · `INVALID_NUMERIC` · `IMPOSSIBLE_VALUE` |
| `detected_at` | timestamp | When the rule fired |

## `energy.silver.weather_forecast_hourly`

Grain: **one valid hour × one point × one forecast vintage.**

| # | Column | Type | Src | Meaning |
|---|---|---|---|---|
| 1 | `region_id` | string | [ing] | `PJM` |
| 2 | `valid_timestamp_utc` | timestamp | [der] | Hour the forecast is *for* |
| 3 | `point_id` | string | [ing] | Load-zone label, e.g. `philadelphia_peco`. Assigned by request order. |
| 4 | `forecast_lead_days` | int | [ing] | **Provenance.** 2 = run from D-1, provably before a next-morning cutoff. 1 = run from day D, not provable. Replaces README section 15's `forecast_run_timestamp_utc`, which the archive does not expose. |
| 5-6 | `latitude`, `longitude` | double | [src] | The **snapped** grid point |
| 7 | `temperature_c` | double | [src] | 2 m air temperature. Available from 2021-03-24. |
| 8 | `apparent_temperature_c` | double | [src] | Combines temperature, humidity and wind. From 2024-01-19. |
| 9 | `dew_point_c` | double | [src] | From 2024-01-19 |
| 10 | `relative_humidity_pct` | double | [src] | From 2024-01-19 |
| 11 | `wind_speed_kmh` | double | [src] | 10 m. From 2024-01-19. |
| 12 | `precipitation_mm` | double | [src] | From 2024-01-19 |
| 13 | `cdd_c` | double | [der] | `max(0, T - 18.33)`. NULL when temperature is NULL — the guard matters, `F.greatest` skips nulls and would report 0 (D-26). |
| 14 | `hdd_c` | double | [der] | `max(0, 18.33 - T)`, same guard |
| 15 | `out_of_bounds_columns` | array\<string\> | [der] | Which measures failed bounds. Per column, so a humidity fault does not cost the temperature reading. |
| 16 | `ingested_at` | string | [ing] | Vintage that survived resolution |

---

## Layer 3 — `energy.gold.daily_summary`

Grain: **one region × one local operating day.**

| Column | Type | Meaning |
|---|---|---|
| `local_date` | date | Operating day, local |
| `region_id` | string | BA |
| `expected_hours` | bigint | **23, 24 or 25**, from the tz-aware calendar. Never hard-coded. |
| `actual_hours` | bigint | Rows present |
| `demand_hours` | bigint | Hours with a *usable* demand value: not null, not flagged |
| `suspect_hours` | bigint | Hours carrying the layer-2 flag |
| `above_record_hours` | bigint | Hours carrying the layer-3 flag |
| `missing_hours` | bigint | Hours where no metric arrived at all |
| `peak_demand_mwh` | double | Max usable demand. Flagged hours excluded — otherwise a 262,651 reading publishes as the all-time peak. |
| `peak_local_hour` | int | Local hour of the peak |
| `peak_timestamp_utc` | timestamp | Canonical instant of the peak |
| `min_demand_mwh`, `mean_demand_mwh` | double | Over usable hours |
| `load_factor` | double | `mean / peak`. ~0.885 for PJM, a stable sanity check. |
| `day_ahead_peak_mwh` | double | Peak of the **aligned** forecast |
| `day_ahead_peak_local_hour` | int | Its hour, found independently of the actual peak |
| `peak_magnitude_error_mwh` | double | `forecast_peak - actual_peak`. **Signed**, so a persistent bias stays visible. |
| `peak_magnitude_error_pct` | double | Same, relative |
| `peak_timing_error_hours` | int | Forecast peak hour minus actual. The column whose value of -1 on 62.4% of days exposed D-28. |
| `is_complete` | boolean | `actual_hours == expected_hours AND demand_hours == expected_hours` |
| `is_weekend`, `day_of_week`, `month`, `year` | | From the calendar dimension |

---

## Layer 4 — `energy.gold.demand_features`

Grain: **one forecast run × one target hour.** 33 declared features plus keys, label and
benchmark.

### Keys and evaluation dimensions

| Column | Meaning |
|---|---|
| `forecast_date` | Day D, the run |
| `cutoff_utc` | 10:00 local on D. **14:00 UTC in EDT, 15:00 in EST** — the instant moves with DST. |
| `target_timestamp_utc` | The hour being predicted |
| `target_local_date`, `target_local_hour` | Local view of the target |
| `hours_ahead` | 14.0 to 38.0. Carried for **evaluation only**: under a fixed cutoff it is a deterministic function of the target hour, so as a feature it is collinear with `target_local_hour` (D-05). |

### Naming convention

`demand_cutoff_minus_3h` is **anchored to the cutoff** — one value per forecast run,
constant across that run's target hours. `demand_same_hour_minus_2d` is **anchored to
the target** — it moves hour by hour. Reading the anchor out of the name is what makes
the leakage audit checkable by eye as well as by test.

| Column | Meaning |
|---|---|
| `demand_cutoff_minus_3h` … `_6h` | Recent level. Starts at **3h, not 1h**: measured D publication lag is ~1.4h, so at a 10:00 cutoff the 09:00 reading does not exist yet (D-24). |
| `demand_cutoff_minus_27h` | Same clock hour as the cutoff, previous day (3 + 24) |
| `demand_rolling_mean_24h_to_cutoff`, `_std_` | 24 hours ending at cutoff-3h |
| `demand_same_hour_minus_2d` / `_7d` / `_14d` | Same clock hour, 2/7/14 days before the target. **48h is the smallest safe target offset** — the maximum horizon is 38h, so `t-24h` is unavailable for horizons past ~21h (D-04). |
| `demand_same_hour_prev_week_mean` | Mean of `t-48h` … `t-168h` at the target clock hour, from explicit lags so each contributor stays auditable |
| `temp_c_<point>_lead2` × 8 | Forecast temperature per load zone, lead-2 vintage. Fed separately rather than pre-averaged, so the model learns the weighting instead of inheriting invented weights (D-08). |
| `temp_c_region_mean_lead2` | Unweighted mean across the eight points |
| `cdd_region_lead2`, `hdd_region_lead2` | Region-mean degree days. Two non-negative terms encode the U-shaped load response that a raw temperature term cannot. |
| `temp_c_target_day_max_lead2`, `_min_` | Target day's extremes. Emitted only for whole days: `SUM` and `MAX` skip nulls, so a partial day would read as a milder one (D-26). |
| `cdd_target_day_sum_lead2`, `hdd_` | Target day's degree-day totals, same completeness guard |

### Label and benchmark — not features

| Column | Meaning |
|---|---|
| `label_demand_mwh` | Actual demand at the target hour. NULL when flagged, so the model never trains on a value the pipeline already doubts. |
| `benchmark_eia_df_mwh` | EIA's **aligned** day-ahead forecast for the same hour — the bar to clear |
| `label_is_suspect`, `label_above_record` | Why a label is NULL, so the drop is reportable rather than silent |

Excluded on purpose: `NG` and `TI`. Their publication lag is ~28h, which leaves even
`t-48h` unavailable for late target hours, and net generation is close to collinear with
demand anyway (D-24).

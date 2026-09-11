# Hourly Incremental Electricity Demand Forecasting Lakehouse

> **Azure Databricks · ADLS Gen2 · EIA-930 · Historical Weather Forecasts · PySpark · XGBoost**

An end-to-end Data Engineering and forecasting project that builds an **hourly incremental electricity-demand lakehouse** on Azure Databricks and evaluates an independent day-ahead forecasting model against the balancing authority's published day-ahead demand forecast.

The project is designed as a **production-minded portfolio project**, not as a notebook-only demo. The primary focus is reliable data ingestion, revision-aware data modeling, time-series correctness, leakage prevention, reproducible backtesting, data quality, and a measurable business outcome.

---

## 1. Business Problem

Electricity operators need to estimate future demand before the operating period begins.

For this project, we frame the problem as:

> **At a fixed 10:00 forecast cutoff on day D, can we produce a credible hourly forecast for all 24 hours of day D+1, and how does it compare with the balancing authority's published day-ahead forecast?**

The project focuses on two operational questions:

1. **How much electricity is expected to be required tomorrow, hour by hour?**
2. **How accurately can we identify tomorrow's peak-demand magnitude and timing?**

The goal is not to claim that this system controls a real electrical grid. It is a **decision-support data product** built from public data.

---

## 2. Why This Project?

Many data-engineering portfolio projects stop at:

```text
CSV
  ↓
Spark
  ↓
Delta
  ↓
Dashboard
```

This project intentionally goes further.

It addresses real issues that matter in production time-series systems:

- external API ingestion
- incremental processing
- raw-data preservation
- upstream data revisions
- data-quality quarantine
- UTC/local-time handling
- daylight saving time (DST)
- forecast-vintage tracking
- strict information-availability rules
- time-series backtesting
- benchmark comparison
- extreme-demand evaluation
- forecast monitoring

The central question is not:

> "Can I use Databricks?"

It is:

> **"Can I build a reproducible data product that produces a forecast without using information that was not actually available at forecast time?"**

---

## 3. Project Scope — V1

### Included

```text
EIA-930
  ↓
ADLS Gen2
  ↓
Databricks Auto Loader
  ↓
Bronze
  ↓
Silver + Data Quality
  ↓
Gold Feature Table
  ↓
Forecast Model
  ↓
Rolling Backtest
  ↓
Forecast Accuracy
  ↓
Power BI
```

### V1 decisions

| Area | Decision |
|---|---|
| Region | PJM |
| Main source | EIA-930 |
| Weather source | Open-Meteo **Previous Runs** API, lead-2 vintage (D-01, D-21) |
| Forecast framing | Day-ahead |
| Cutoff | 10:00 local operating date, represented canonically in UTC |
| Target | every hour of local operating day D+1 — **23, 24 or 25** of them |
| Core model | XGBoost |
| Primary external benchmark | EIA/BA day-ahead forecast (`DF`) |
| Secondary baseline | Seasonal-naive same-hour previous-week |
| Evaluation | Rolling-origin backtesting |
| Primary metrics | MAE, RMSE, MAPE |
| Peak metrics | Peak magnitude error, peak timing error |
| Extreme regime | Top 5% of each fold's **training** labels (D-29) |
| Training window | 2021-03-24 onward — set by the weather archive, not EIA (D-21) |
| Serving | Power BI over Gold tables |

---

## 4. Data Sources

### 4.1 EIA-930 — Hourly Electric Grid Monitor

Primary source:

**U.S. Energy Information Administration (EIA), Form EIA-930**

The dataset provides hourly operating data by balancing authority, including:

- actual demand
- day-ahead demand forecast
- net generation
- interchange

EIA provides this data through its Open Data API and bulk-download mechanisms.

Measured: the API route serves from **2019-01-01**, not from the start of
Form EIA-930. The route publishes a single measure column (`value`) in **long**
form, so one hour arrives as four rows discriminated by `type` (D-02).

Official resources:

- https://www.eia.gov/opendata/index.php/api
- https://www.eia.gov/opendata/browser/electricity/rto/region-data

### 4.2 Weather Forecast Data

The forecasting model should use **weather forecasts that would have been available at the forecast cutoff**, not hindsight weather observations.

**Decided: the Previous Runs API.** The Historical Forecast API is a trap — it
stitches together short lead-time forecasts, so for a target 14-38 hours past the
cutoff it is far more accurate than anything that existed at the cutoff. It would leak
while passing every check in section 8 (D-01). It is used only as *observed* weather
for post-hoc analysis.

- Previous Runs API (**used**):
  https://open-meteo.com/en/docs/previous-runs-api
- Historical Forecast API (observed weather only):
  https://open-meteo.com/en/docs/historical-forecast-api

Measured archive floors, which differ per variable and set the training window (D-21):

```text
temperature_2m          2021-03-24     ~1,994 days
everything else         2024-01-19     ~  963 days
```

Temperature reaches roughly twice as far back, so the primary feature set uses
temperature via CDD/HDD and treats humidity, wind and precipitation as an ablation on
the shorter window.

For historical backtesting, a weather run is only eligible when:

```text
weather_run_timestamp <= forecast_cutoff_timestamp
```

If multiple eligible runs exist, the latest eligible run is selected.

---

## 5. Forecast Definition

The project uses a **day-ahead forecasting contract**.

For each forecast run:

```text
Forecast date D
Cutoff C = 10:00 on D

Target:
every hour of the local operating day D+1
```

That is **23, 24 or 25 hours**, read from a tz-aware calendar. Hard-coding 24 is wrong
twice a year, and the EIA feed itself loses most of its demand data on those dates —
24.2% NULL on DST days against 0.051% otherwise (D-19).

The resulting forecast horizons are approximately:

```text
14 hours → 38 hours ahead
```

The exact horizon depends on the target hour and the timestamp conventions used by the source.

### Information availability rule

At cutoff `C`, the model may only use information that was available by `C`.

Example:

```text
At 10:00 Monday

AVAILABLE
- electricity observations through the latest valid timestamp before cutoff
- historical demand
- calendar information
- weather forecast runs issued no later than cutoff

NOT AVAILABLE
- Monday 18:00 actual demand
- Tuesday actual weather
- weather forecast runs issued after cutoff
- any revised information that could only have been known after cutoff
```

This rule is the foundation of leakage prevention.

---

## 6. Benchmark Strategy

The project deliberately separates the benchmark from the model.

### Benchmark 0 — Seasonal Naive

For each target hour:

```text
Prediction = same hour from the previous week
```

Conceptually:

```text
ŷ(D+1, h) = y(D-6, h)
```

This is a stronger and more honest seasonal baseline than a simple lag-24 forecast under a day-ahead cutoff.

### Benchmark 1 — EIA / Balancing Authority Day-Ahead Forecast

EIA-930 publishes a day-ahead demand forecast (`DF`).

This is the primary real-world benchmark.

The model does **not** use `DF` as an input in the core experiment.

```text
EIA DF = benchmark
Our XGBoost = challenger
```

### Optional V2 — Residual Correction

A separate experiment may treat EIA's forecast as an input:

```text
EIA DF
  ↓
forecast residual
  ↓
correction model
  ↓
corrected forecast
```

This is a different problem from independent forecasting and will be reported separately.

---

## 7. Core Forecast Model

### V1 model

**XGBoost**

Features are generated from information available at the forecast cutoff.

### Forecast-origin features

#### Recent demand

```text
demand[C-3h]   demand[C-4h]   demand[C-5h]   demand[C-6h]   demand[C-27h]
```

Starting at 3h, not 1h. **`demand[C-1h]` does not exist**: measured publication lag on
`D` is ~1.4h, so at a 10:00 cutoff the 09:00 reading has not been published yet. Lag is
per metric — `NG` and `TI` lag ~28h, which leaves them useless at this horizon and they
are excluded from the feature set entirely (D-24).

#### Cutoff-anchored rolling features

```text
rolling_mean(C-24h → C-1h)
rolling_std(C-24h → C-1h)
```

#### Same-hour historical demand

For a target hour `h` on D+1:

```text
demand(target - 48h)    demand(target - 168h)    demand(target - 336h)
```

Expressed as absolute offsets from the target, never as "N days ago": the anchor is
where leakage enters. **48h is the smallest safe target offset** — the maximum horizon
is 38h, so `target - 24h` is unavailable for any horizon past ~21h (D-04).

#### Calendar features

```text
hour
day_of_week
month
day_of_year
is_weekend
is_holiday
```

#### Weather forecast features

```text
temp_c_<point>_lead2   x 8 load zones
temp_c_region_mean_lead2
temp_c_target_day_max_lead2 / _min_
```

Temperature only in the primary set. Humidity, wind and precipitation exist in the
archive only from 2024-01-19 and taking them would halve the training window for a
weak marginal signal (D-21). The eight zone points are fed **separately** rather than
pre-averaged, so the model learns the weighting instead of inheriting invented weights
(D-08).

#### Weather-derived features

```text
CDD  # Cooling Degree Days
HDD  # Heating Degree Days
```

CDD/HDD are used because electricity demand often responds non-linearly to temperature: both unusually hot and unusually cold conditions can increase demand.

---

## 8. Feature Leakage Rules

The project treats leakage as a **data-contract problem**, not just a modeling convention.

### Forbidden examples

For a forecast generated at Monday 10:00:

```text
Monday 18:00 actual demand        ❌
Tuesday actual temperature       ❌
Tuesday weather observation      ❌
Tuesday 12:00 weather run        ❌
```

### Valid examples

```text
Monday 09:00 actual demand       ✅
Monday 06:00 weather forecast    ✅
Tuesday calendar                 ✅
Historical demand from prior days ✅
```

The feature-generation code must be driven by the forecast cutoff rather than by the target timestamp.

---

## 9. Data Architecture

```text
                    ┌────────────────────┐
                    │      EIA-930       │
                    │ D / DF / NG / TI   │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │ Archived Weather    │
                    │ Forecasts           │
                    └─────────┬──────────┘
                              │
                              ▼
                       ┌─────────────┐
                       │  ADLS Gen2  │
                       │   Landing   │
                       └──────┬──────┘
                              │
                              ▼
                   ┌─────────────────────┐
                   │ Azure Databricks    │
                   │ Auto Loader / Spark │
                   └──────────┬──────────┘
                              │
                              ▼
                         ┌──────────┐
                         │ BRONZE   │
                         │ Raw      │
                         └────┬─────┘
                              │
                              ▼
                         ┌──────────┐
                         │ SILVER   │
                         │ Cleaned  │
                         │ + DQ     │
                         └────┬─────┘
                              │
                     ┌────────┴─────────┐
                     │                  │
                     ▼                  ▼
              Demand Features      Weather Features
                     │                  │
                     └────────┬─────────┘
                              ▼
                         ┌──────────┐
                         │  GOLD    │
                         │ Features │
                         │ KPI      │
                         └────┬─────┘
                              │
                              ▼
                      ┌─────────────────┐
                      │ XGBoost Model   │
                      └────────┬────────┘
                               │
                               ▼
                      ┌─────────────────┐
                      │ Forecast Output │
                      └────────┬────────┘
                               │
                ┌──────────────┴──────────────┐
                │                             │
                ▼                             ▼
          Backtesting                    Monitoring
                │                             │
                └──────────────┬──────────────┘
                               ▼
                          Power BI
```

---

## 10. Storage Layout

Example ADLS layout:

```text
/landing
  /eia
    /region-data
      /respondent=PJM
        /date=YYYY-MM-DD

  /weather
    /region=PJM
      /date=YYYY-MM-DD
```

The exact path convention may change during implementation, but partitions should support efficient incremental ingestion and clear data ownership.

---

## 11. Databricks Catalog Structure

Recommended Unity Catalog layout:

```text
energy
├── bronze
│   ├── eia_region_data
│   └── weather_forecast_raw
│
├── silver
│   ├── electricity_hourly
│   ├── weather_forecast_hourly
│   └── electricity_quarantine
│
├── gold
│   ├── demand_features
│   ├── daily_summary
│   ├── forecast_run
│   ├── forecast_prediction
│   └── forecast_accuracy
│
└── ml
    └── model-related assets
```

Bronze is source-oriented.

Silver is domain-oriented and validated.

Gold is business/model-consumption oriented.

---

## 12. Bronze Data Model

### `energy.bronze.eia_region_data`

Logical grain:

```text
1 source record × 1 ingestion event
```

Suggested columns:

```text
period
respondent
respondent_name
type
type_name
value
timezone

source
source_file
ingested_at
```

### Important rule: Bronze is append-only

EIA data can be revised.

Therefore, Bronze must preserve historical vintages.

Example:

```text
period      respondent   type   value   ingested_at
---------------------------------------------------------
10:00       PJM          D      95000   11:00
10:00       PJM          D      95700   14:00   <-- revision
```

These are not necessarily duplicates.

Both records remain in Bronze.

---

## 13. Revision Handling

The pipeline distinguishes:

### Exact duplicate

Same source record and same value.

```text
→ can be deduplicated
```

### Revision

Same logical key, different published value.

```text
(period, respondent, type)
```

but:

```text
value_v1 != value_v2
```

Revision handling:

```text
Bronze
  → preserve all vintages

Silver
  → select latest valid vintage for analytical use
```

Suggested Silver logic:

```text
ROW_NUMBER() OVER (
    PARTITION BY period, respondent, type
    ORDER BY ingested_at DESC
)
```

Additional metadata can include:

```text
revision_count
latest_ingested_at
is_revision
```

This allows the project to measure upstream-data revisions instead of silently hiding them.

---

## 14. Silver Data Model

### `energy.silver.electricity_hourly`

Logical grain:

```text
1 region × 1 event hour
```

Suggested schema:

```text
event_timestamp_utc
region_id

actual_demand_mwh
day_ahead_forecast_mwh
net_generation_mwh
interchange_mwh

ingested_at
```

The exact physical unit naming will be finalized from the source metadata and documented consistently. Do not assume that an hourly energy quantity and average power are interchangeable without defining the semantic conversion.

---

## 15. Weather Silver Data Model

### `energy.silver.weather_forecast_hourly`

Suggested schema:

```text
forecast_lead_days
valid_timestamp_utc

region_id
point_id
latitude
longitude

temperature
dew_point
humidity
wind_speed
precipitation

source_model
source
ingested_at
```

The key concept is that the hour a forecast is **for** and the run it came **from** are
different things, and only the second one bounds availability.

`forecast_run_timestamp_utc` from the original design is **not populable honestly**: the
archive exposes "N days earlier", not a run instant. `forecast_lead_days` replaces it
rather than fabricating a timestamp the leakage audit would then assert against (D-21).

```text
lead 2   run from D-1   provably before a 10:00 cutoff on D   <- primary
lead 1   run from D     very likely, not provable             <- sensitivity only
```

Both vintages are ingested. Measured cost of insisting on the provable one:
**+1.01% MAE** (D-30).

---

## 16. Data Quality

The pipeline should fail loudly or quarantine data when quality contracts are broken.

### Basic checks

```text
event_timestamp IS NOT NULL
region_id IS NOT NULL
numeric cast succeeds
demand > 0
```

### Uniqueness

Logical uniqueness in Silver:

```text
(region_id, event_timestamp_utc)
```

### Completeness

Expected hourly coverage should be compared with actual rows.

Example:

```text
expected = 23 | 24 | 25     <- from a tz-aware calendar, never hard-coded
actual   = rows present
```

Two traps found while implementing this (D-03): subtracting two aware datetimes sharing
one `ZoneInfo` gives **wall-clock** arithmetic, so every DST day reports 24 hours unless
both ends are converted to UTC first; and Open-Meteo called with a named timezone
returns exactly 24 rows on DST days, normalising the discontinuity away.

Missing observations should be explicitly represented or flagged, not silently dropped.

### Range sanity checks

Use a two-layer approach:

1. hard sanity bounds for impossible values
2. statistical anomaly detection for unusual but possible values

Do not reject legitimate extreme demand solely because it is unusual.

---

## 17. Quarantine

Invalid records go to:

### `energy.silver.electricity_quarantine`

Suggested schema:

```text
source_file
event_timestamp
region_id
raw_value
reason
detected_at
```

Example reasons:

```text
INVALID_TIMESTAMP
INVALID_NUMERIC
NEGATIVE_DEMAND
DUPLICATE_SOURCE_RECORD
MISSING_REQUIRED_FIELD
IMPOSSIBLE_VALUE
```

The principle is:

> **Bad data should be observable and recoverable, not silently discarded.**

---

## 18. Time and DST Strategy

Time handling is a first-class concern.

### Canonical time

All event identity and joins use:

```text
event_timestamp_utc
```

### Derived local time

Keep:

```text
local_timestamp
local_date
local_hour
timezone
```

UTC is the canonical key because local time can contain:

- repeated hours during the autumn DST transition
- missing hours during the spring DST transition

Example:

```text
01:00 EDT
01:00 EST
```

These are two different instants.

They must not collapse into one logical row.

---

## 19. Extreme Regimes

Extreme-demand analysis is performed using **actual demand**, not forecast values.

This avoids circular definitions.

### Demand regime

```text
Extreme demand =
actual demand above a historical P95 threshold
```

The threshold is the 95th percentile of **each fold's training labels**. Deriving it
from the whole series would let the test period define what counts as extreme within
itself.

One consequence, stated because it affects a table in the results: expanding-window and
sliding-window variants see different training data, so they set different thresholds
and classify different rows as extreme. Their extreme-regime slices are **not**
comparable with each other (D-29).

### Weather regimes

Optional stress labels:

```text
Heat regime
Cold regime
```

For a robust implementation, region-specific historical percentiles are preferred over universal temperature thresholds.

---

## 20. Stress Test — Winter Storm Elliott

Winter Storm Elliott occurred in December 2022 and affected PJM operations.

PJM's published material reports a significant difference between forecasted and actual load during the event.

The project uses this event as a **historical stress-test case**, not as evidence that the project model is correct.

Evaluation questions:

```text
How did the model behave during the event?

Did it:
- capture the direction of demand?
- identify the peak window?
- under-forecast extreme demand?
- outperform or underperform the EIA benchmark?
```

A dedicated case-study section can be added to the final README after the model is evaluated.

---

## 21. Backtesting

A single train/test split is not sufficient for this project.

Use **rolling-origin backtesting**.

Conceptually:

```text
Fold 1
TRAIN ────────────────── TEST

Fold 2
TRAIN ─────────────────────── TEST

Fold 3
TRAIN ───────────────────────────── TEST

Fold 4
TRAIN ───────────────────────────────── TEST
```

Each fold respects chronological ordering.

No random shuffling.

---

## 22. Evaluation Dimensions

Model performance will be evaluated by:

### Overall

```text
MAE
RMSE
MAPE
```

### Forecast horizon

```text
hours_ahead
```

### Hour of day

```text
00:00
01:00
...
23:00
```

### Day type

```text
weekday
weekend
holiday
```

### Demand regime

```text
normal
extreme
```

### Peak behavior

```text
peak magnitude error
peak timing error
peak hit rate
```

---

## 23. Forecast Data Model

### `energy.gold.forecast_run`

One row represents one forecasting event.

```text
forecast_run_id
region_id

cutoff_timestamp_utc

model_name
model_version

weather_run_timestamp_utc

training_data_end_timestamp

created_at
```

### `energy.gold.forecast_prediction`

One row represents one target hour.

```text
forecast_run_id
target_timestamp_utc

hours_ahead

predicted_p50
```

V2 may add:

```text
predicted_p10
predicted_p90
```

### `energy.gold.forecast_accuracy`

Populated after actual demand becomes available.

```text
forecast_run_id
target_timestamp_utc
region_id

forecast_mwh
actual_mwh

error_mwh
absolute_error_mwh
ape

model_name
model_version

hours_ahead
hour_of_day

is_extreme
is_peak
is_in_sample
```

`is_in_sample` marks a target the served model trained on. See section 25 — without it
the table published a fabricated 45% win over the benchmark.

### Where the model lives — MLflow in Unity Catalog

`energy.ml.demand_forecaster`, registered by a separate `energy_monthly_refit` job.
`model_version` on every prediction row points back to the artifact that produced it.

Two cadences, because they differ by a factor of thirty:

```text
energy_monthly_refit    fit, score, register, maybe promote     monthly
energy_daily_pipeline   load @champion, predict, score          daily
```

**Registering is not promoting.** Drift was measured here — demand rose 11.5% across the
backtest and the model's bias tracked it (section 32) — so a refit is not automatically
an improvement. Each run registers a version; the alias moves only if the candidate is
within 2% of the incumbent. Without that band, month-to-month noise alone flips the
served model.

**The incumbent is re-scored, not looked up.** This used to read "the incumbent's
*recorded* score", and that was the bug: the holdout starts at a month boundary and ends
wherever the data does, so each refit measured on a superset of the last. The first time
it mattered, 7.5% of the evaluation rows were present on one side and absent on the
other, and the gate declined a candidate over it. Re-scoring the incumbent showed the
model had never been worse — the added three days simply carried an MAE of 4,584 against
3,409 on the rest. The incumbent's gate is therefore refitted on its own training window
and scored on the candidate's holdout, at decision time (D-43, D-44).

**Two models are fitted, deliberately:**

| | trained on | role |
|---|---|---|
| gate | everything before a 2-month holdout | scored out-of-sample; decides promotion |
| served | all labelled data | registered, and what actually predicts |

Registering only the gate model was tried first and measured: it cost **+9.1% MAE**
(4,144.7 → 4,523.9), because under drift the withheld months carry the most
information. The metric is therefore named `gate_holdout_mae`, which settles *whose*
score it is for free. It does not settle *what it was measured on* — that took re-scoring
the incumbent, and a comment in `registry.py` claiming the metric was already compared
like-for-like sat there being wrong in the meantime. A good name is not a measurement.

**The wrapper carries a contract.** An XGBoost Booster remembers neither which columns
it trained on nor their order, and binds by position once a DMatrix is built — handed a
reordered or short frame it returns numbers, not an error. The pyfunc wrapper stores the
feature list, reindexes to it, and raises on a missing feature.

---

## 24. Peak Detection

For every forecast day:

```text
predicted_peak_timestamp
predicted_peak_demand
```

After actuals become available:

```text
actual_peak_timestamp
actual_peak_demand
```

Metrics:

```text
peak_magnitude_error
peak_magnitude_error_pct
peak_timing_error_hours
peak_hit
```

Example:

```text
Actual peak:
18:00

Predicted peak:
19:00

Peak timing error:
1 hour
```

---

## 25. Monitoring

The project includes an operational forecast-accuracy layer.

Once the actual target hour has occurred:

```text
forecast
   ↓
actual arrives
   ↓
calculate error
   ↓
write forecast_accuracy
   ↓
monitor
```

This enables analysis such as:

```text
Which hours have the largest errors?

Does the model degrade during extreme demand?

Does performance drift by season?

Does the model consistently under-forecast?
```

### This table measures operations, not capability

Two tables were being conflated, and the conflation manufactured a result (D-37):

| | measures | source |
|---|---|---|
| rolling backtest, 55 folds | **capability** — genuinely out-of-sample | `scripts/run_backtest.py` |
| `gold.forecast_accuracy` | **operations** — forecasts issued, later scored | the daily job |

The served model is fitted on all labelled data, so a target it already trained on is
not a test of anything. Scored that way it posted **MAE 1,573 against the benchmark's
2,856** — a 45% "win" over the balancing authority, entirely fabricated.

Every row therefore carries `is_in_sample`, computed against the cutoff the **artifact
itself recorded** (`served_model_max_label_ts`), never against a local assumption about
it. The summary reports the out-of-sample split first; an empty split is a real answer,
meaning the model has not yet predicted an hour it had not already seen.

Getting that cutoff right took three attempts, each failing in the direction that
flattered the model. A guard against leakage has to read the exact quantity, not a
proxy for it — a wrong proxy does not raise, it silently widens the gate.

This monitoring layer is intentionally prioritized over adding unnecessary streaming infrastructure.

---

## 26. Power BI

V1 uses one business-facing page.

### KPI cards

```text
Current / latest demand
Next-day predicted peak
Forecast MAE
Peak timing error
```

### Main chart

```text
Actual demand
EIA day-ahead forecast
Our model
```

### Peak section

```text
Predicted peak
Actual peak
Peak error
```

### Benchmark section

```text
Seasonal Naive
EIA DF
XGBoost Challenger
```

The dashboard consumes Gold tables, not raw Bronze/Silver data.

---

## 27. Cost-Conscious Architecture

The project intentionally avoids continuous compute in V1.

### V1 ingestion

```text
External Python ingestion
        ↓
ADLS
        ↓
Databricks triggered processing
```

### V1 processing

Prefer:

```text
triggered batch
```

over:

```text
24/7 continuous streaming
```

The objective is to demonstrate **incremental data engineering**, not to consume compute simply to say "streaming".

Kafka, Event Hubs, Terraform, DAB/CI-CD, model serving, and other production extensions are deliberately postponed until the core pipeline is complete.

---

## 28. V1 Architecture Boundaries

### V1 includes

```text
[x] EIA-930 ingestion            271,989 rows, 2019-01-01 ->
[x] Weather forecast ingestion   765,696 rows, 8 points x 2 vintages
[x] ADLS Gen2                    container + external location + catalog
[x] Auto Loader                  directory listing, availableNow, idempotent
[x] Bronze                       eia 271,989 · weather 777,600, on ADLS via UC
[x] Silver                       67,375 rows, gap-free UTC spine
[x] Data Quality                 three layers: quarantine / flag / ceiling
[x] Quarantine                   7 rows, reason + original text
[x] Revision handling            vintage-resolved, revisions counted not hidden
[x] Gold feature table           47,832 rows, 33 features, 0 leakage violations
[x] Seasonal-naive benchmark     lag-168
[x] EIA DF benchmark             hour-aligned first (D-28)
[x] XGBoost challenger           A / B ablation + 3 variants
[x] Rolling backtesting          55 monthly folds
[x] Peak evaluation              magnitude, timing, hit rate
[x] Forecast accuracy monitoring 3 tables, 158,061 scored hours
[~] One Power BI page            spec + DAX + preview; .pbix is authored in Desktop
```

### V2 candidates

```text
[ ] ERCOT
[ ] CAISO
[ ] Population-weighted weather
[ ] Quantile forecasting (P10/P50/P90)
[ ] Conformal prediction
[ ] Residual correction model
[ ] Asymmetric loss
[ ] GitHub Actions
[ ] Terraform
[x] Databricks Asset Bundles     dev/prod targets, real catalog + checkpoint isolation
[x] Model registry (MLflow + UC) energy.ml.demand_forecaster, promotion gated
[x] Model versioning             model_version on every prediction row
[ ] CI/CD                        needs a service principal to own prod
[ ] Streaming architecture
```

---

## 29. Repository Structure

```text
energy-demand-lakehouse/
│
├── README.md
│
├── src/
│   ├── ingestion/
│   │   ├── eia_client.py
│   │   └── weather_client.py
│   │
│   ├── bronze/
│   │   ├── eia.py
│   │   └── weather.py
│   │
│   ├── silver/
│   │   ├── electricity.py
│   │   └── weather.py
│   │
│   ├── gold/
│   │   ├── features.py
│   │   ├── daily_summary.py
│   │   └── forecast_accuracy.py
│   │
│   └── ml/
│       ├── features.py
│       ├── train.py
│       ├── predict.py
│       └── evaluate.py
│
├── scripts/
│   └── validate_sources.py
│
├── tests/
│
├── sql/
│
└── docs/
    ├── architecture.md
    ├── data-dictionary.md
    ├── data-quality.md
    └── forecasting.md
```

---

## 30. Development Sequence

Do not build the whole platform at once.

### Milestone 0 — Source Validation

Answer:

```text
1. Does PJM exist in EIA facets?
2. Does type=D exist?
3. Does type=DF exist?
4. What is the actual DF historical coverage?
5. What is the usable historical weather-forecast coverage?
```

This determines the realistic training/evaluation period.

---

### Milestone 1 — EIA → Bronze

```text
EIA API
  ↓
Python extractor
  ↓
ADLS landing
  ↓
Auto Loader
  ↓
Bronze Delta
```

Required capabilities:

```text
pagination
retry
incremental extraction
raw preservation
ingestion metadata
```

---

### Milestone 2 — Bronze → Silver

Implement:

```text
schema normalization
type casting
revision handling
deduplication
timestamp normalization
data-quality checks
quarantine
```

---

### Milestone 3 — Gold

Build:

```text
hourly demand
daily summary
peak metrics
feature table
```

---

### Milestone 4 — Forecasting

Implement:

```text
seasonal naive
EIA DF benchmark
XGBoost challenger
rolling backtest
error analysis
```

---

### Milestone 5 — Monitoring + BI

Build:

```text
forecast_accuracy
peak evaluation
Power BI
```

Stop here for V1.

---

## 31. Definition of Done

V1 is complete when all of the following are true:

- [x] Raw EIA data can be reproduced from the documented source. — `eia_client.py`, backfill + incremental
- [x] PJM data is loaded incrementally into Bronze. — Auto Loader on ADLS, re-run adds **0 rows** (asserted in the notebook)
- [x] Revisions are not incorrectly classified as duplicates. — `revision_count` counts distinct *published values*
- [x] Silver contains one analytical record per logical region/hour. — 67,375 on a gap-free UTC spine
- [x] Invalid records are observable through quarantine. — 7 rows with reason and original text
- [x] UTC is the canonical event time. — established by measurement, not assumption (D-13)
- [x] Historical weather forecasts respect the forecast cutoff. — lead-2 vintage, provably pre-cutoff
- [x] Feature generation passes a leakage audit. — **47,832 pairs, 0 violations**, and 5 known-leaky features asserted caught
- [x] A seasonal-naive benchmark exists. — lag-168, MAPE 8.560%
- [x] EIA day-ahead forecast is benchmarked. — MAPE 2.475%, **after correcting an hour-label offset** (D-28)
- [x] XGBoost is evaluated using rolling-origin backtesting. — 55 monthly folds, 39,476 hours per model
- [x] Results are split by normal/extreme regime. — threshold from each fold's training labels
- [x] Peak magnitude and timing are evaluated. — magnitude MAPE, timing MAE, exact-hour hit rate
- [x] Forecast accuracy is stored after actuals arrive. — `forecast_run` / `forecast_prediction` / `forecast_accuracy`, with `is_in_sample` so operational scoring cannot be mistaken for a capability measure
- [~] A single Power BI page exposes the main business metrics. — tables, data model, DAX and a rendered layout preview exist; the `.pbix` itself is a proprietary binary and is authored in Power BI Desktop ([`docs/powerbi.md`](docs/powerbi.md))
- [x] README and architecture documentation are reproducible. — `docs/decisions.md` D-01..D-30, diagram generated from code

---

## 32. Final Success Criteria

The project did **not** assume an accuracy number before training. Backtest executed
2026-09-10; the numbers below are measured, and the headline is a loss.

### Measured result

Rolling-origin, 55 monthly folds, 2022-03 → 2026-09, 39,476 scoreable hours per model,
every model scored on identical rows.

| Model | MAE MWh | RMSE | MAPE | vs benchmark |
|---|---|---|---|---|
| seasonal naive (lag-168) | 8,300 | 11,410 | 8.560 % | +261.8 % |
| **EIA DF — benchmark** | **2,294** | **2,934** | **2.475 %** | — |
| xgb_a — no weather | 5,203 | 7,211 | 5.299 % | +126.8 % |
| xgb_b — with weather | 2,581 | 3,545 | 2.678 % | **+12.5 %** |

**The independent challenger does not beat the balancing authority's own day-ahead
forecast.** It lands within 12.5% of it on public data alone, and loses on peak timing
too (48.4% exact-hour against 64.7%).

### What weather is worth

Same window, same folds, same rows; only the feature set differs.

```text
xgb_a  18 features, no weather    MAE 5,203    MAPE 5.299%
xgb_b  33 features, + weather     MAE 2,581    MAPE 2.678%
                                  MAE -50.4%,  MAPE -49.5%
```

Larger again in the extreme regime, 9.029% → 3.375% MAPE. This is the clean result.

### Why it loses, diagnosed rather than guessed

```text
PJM demand      2022 mean 90,865 MWh  ->  2026 mean 101,336     +11.5%
xgb_b bias      2022 -497            ->  2026 -1,998
corr(training rows, bias) = -0.585
```

Concept drift. The benchmark is also biased low but **flat**; the challenger drifts,
because an expanding window drags 2021 levels into a 2026 prediction.

Two fixes were named before the numbers were seen — a 24-month sliding window and a
ratio target. Both **reduced the bias they targeted** (2026: -1,998 → -997) and both
**made accuracy worse**, a bias-variance trade. Plain `xgb_b` remains best. The list
stopped there; with 55 folds, sweeping until something wins fits the backtest rather
than the problem.

The remaining gap is not self-inflicted caution: using the *unprovable* fresher weather
vintage recovers only **1.01% MAE** (D-30). What is left is information the challenger
structurally does not have — PJM's own outage schedule, large-customer notifications,
and a daily reforecast against a monthly refit.

### Success is therefore defined as:

### Data Engineering

> Build a reproducible, incremental, revision-aware lakehouse pipeline.

### Forecasting

> Produce a leakage-free day-ahead forecast using only information available at the cutoff.

### Benchmarking

> Compare the challenger fairly against both a seasonal-naive baseline and the balancing authority's published day-ahead forecast.

### Business usefulness

> Measure peak-demand magnitude and timing, not just generic model accuracy.

### Reliability

> Demonstrate how forecast performance changes across normal and extreme-demand regimes.

Full reasoning, including four places where an earlier conclusion was withdrawn once
data contradicted it, is in [`docs/decisions.md`](docs/decisions.md) (D-01 … D-30).

---

## 33. Expected Interview Story

A concise description of the project:

> Built an hourly incremental electricity-demand lakehouse on Azure Databricks using EIA-930 and archived weather forecasts. Implemented Bronze/Silver/Gold layers, revision-aware ingestion, data-quality quarantine, DST-safe time handling, forecast-origin feature engineering, and rolling-origin backtesting. Developed an independent XGBoost day-ahead forecast and benchmarked it against the balancing authority's published day-ahead forecast, including peak-demand and extreme-regime analysis.

The strongest interview topics are:

```text
Why is DF a benchmark and not a feature?
Why is lag-24 not a valid generic feature here?
How did you prevent weather leakage?
How did you handle EIA revisions?
Why is UTC the canonical timestamp?
How did you handle DST?
Why rolling-origin backtesting?
How do you define an extreme event without leakage?
Why triggered batch instead of streaming?
How do you know the model adds value?
```

---

## 34. References

### EIA

- EIA Open Data API  
  https://www.eia.gov/opendata/index.php/api

- EIA-930 / RTO hourly region data  
  https://www.eia.gov/opendata/browser/electricity/rto/region-data

### Weather

- Open-Meteo Historical Forecast API  
  https://open-meteo.com/en/docs/historical-forecast-api

- Open-Meteo Previous Runs API  
  https://open-meteo.com/en/docs/previous-runs-api

- Open-Meteo Single Runs API  
  https://open-meteo.com/en/docs/single-runs-api

### Azure Databricks

- Medallion Architecture  
  https://learn.microsoft.com/en-us/azure/databricks/lakehouse/medallion

- Auto Loader  
  https://learn.microsoft.com/en-us/azure/databricks/ingestion/cloud-object-storage/auto-loader/

### PJM

- PJM Territory Served  
  https://www.pjm.com/about-pjm/who-we-are/territory-served

- Winter Storm Elliott  
  https://www.pjm.com/markets-and-operations/winter-storm-elliott

---

## 35. Status

**Current status: V1 complete.**

```text
[x] M0  source validation          floors measured, timezone established by evidence
[x] M1a ingestion                  EIA 271,989 rows · weather 765,696 rows
[x] M1b Auto Loader -> Bronze      serverless job, 60-94 s per run, idempotent
[x] M2  Silver + DQ + quarantine   67,375 rows, three DQ layers
[x] M3  Gold daily summary         2,809 operating days, DST-correct
[x] M4  Feature table + audit      47,832 rows, 0 leakage violations
[x] M5  Models + backtest          55 folds, 4 models, 3 variants
[x] M6  forecast_accuracy, Elliott case study, Power BI spec + preview
[x] +   Asset Bundles — dev/prod targets, isolated catalogs and checkpoints
[x] +   MLflow registry in UC — monthly refit, gated promotion, versioned predictions
```

149 tests passing. ~90% of the work ran locally at $0; the Databricks portion is a
handful of serverless job runs of roughly one to five minutes each.

Three proposals of mine were withdrawn after measurement rather than quietly kept:
the registry would make the daily job faster (it did not — the task was never
fit-bound), registering only the scored model was the honest choice (it cost 9.1% MAE),
and dependency installation was the removable overhead (there was none to remove; ~35 s
of notebook work and a 40-60 s serverless floor). All three are in
[`docs/decisions.md`](docs/decisions.md) with the numbers, D-36 to D-38.

Local and cloud agree exactly, which is what makes the local-first approach a method
rather than a shortcut:

```text
                          Databricks Bronze     local landing
eia_region_data                 271,989           271,989
  distinct source files           2,839             2,839
  keys with >1 vintage            2,976             2,976
weather_forecast                777,600           777,600
  distinct source files           2,025             2,025
  after revision resolution       765,696           765,696
```

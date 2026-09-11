# Decision log

Decisions that the README leaves open, resolved before pipeline code was written.
Each entry records the reason, because the reason is the interview answer.

---

## D-01 Weather source: Previous Runs API, not Historical Forecast API

**Decision.** Model features come from the Open-Meteo **Previous Runs API**
(`temperature_2m_previous_dayN`). The **Historical Forecast API** is used only as
*observed* weather for post-hoc error analysis, never as a model feature.

**Reason.** The Historical Forecast archive is a stitched series assembled from
short lead-time forecasts. For a target 14-38 hours past the cutoff it is far more
accurate than anything that existed at the cutoff, so using it would leak while
still passing every check in README section 8. Previous Runs exposes an explicit
vintage per valid timestamp, which is what the availability rule actually requires.

**Open sub-decision.** `previous_day1` is the run from day D; `previous_day2` is
from D-1. The exact archive run hour is not documented, so if `previous_day1`
cannot be shown to be issued before the 10:00 ET cutoff, the core experiment uses
`previous_day2` and reports `previous_day1` separately as a sensitivity analysis.
Quantifying the cost of the safety margin is a stronger result than assuming it away.

---

## D-02 EIA hourly starts 2019-01-01, not 2015-07 — weather ablation, not two windows

Measured 2026-09-08 with a live key. The API route reports
`startPeriod = 2019-01-01T00`, and PJM row counts confirm it:

| Year | D | DF | DF coverage |
|---|---|---|---|
| 2015-2018 | 0 | 0 | route does not serve it |
| 2019 | 8,760 | 8,713 | 99.5% |
| 2021 | 8,760 | 8,760 | 100.0% |
| 2024 | 8,784 | 8,736 | 99.5% |
| 2025 | 8,760 | 8,735 | 99.7% |

| Source | First usable | Span |
|---|---|---|
| EIA-930 via API v2 | **2019-01-01** | ~2,442 days |
| Open-Meteo Previous Runs | 2021-03-24 | ~1,994 days |
| **Gap** | | **~448 days** |

**Decision.** The earlier plan — a no-weather model on six extra years versus a
weather model on fewer — is withdrawn. The gap is 1.2 years, not 5.7, so that
comparison no longer answers anything interesting.

Replaced by a clean **ablation on one shared window** (2021-03-24 onward):

- **Model A** — lag + calendar
- **Model B** — lag + calendar + weather

Same rows, same folds, only the feature set differs. This measures the marginal
value of weather directly, which is the number worth reporting.

**Rejected.** Backfilling 2015-2018 from the EIA Grid Monitor bulk CSVs. Different
schema, a second ingestion path, and three years that have no matching weather
vintages anyway. The floor stays 2019-01-01.

**Follow-on.** DF is missing ~25-45 hours per year. Those hours are excluded from the
three-way comparison rather than scored as DF errors, and the exclusion count is
reported alongside the metrics.

## D-03 A local operating day is 23, 24 or 25 hours

README sections 5 and 16 assume 24 target hours per day. Measured:

```
2023-03-12  ->  23 hours
2023-07-01  ->  24 hours
2023-11-05  ->  25 hours
2026-03-08  ->  23 hours
2026-11-01  ->  25 hours
```

**Decision.** `expected_hours` is derived from a tz-aware calendar per operating
date, never hard-coded. The completeness check and the peak-detection window both
read it.

**Two traps found while proving this.**

1. Subtracting two aware `datetime`s that share the same `ZoneInfo` yields
   wall-clock arithmetic, not elapsed time. Both sides must be converted to UTC
   first, or every DST day silently reports 24 hours.
2. Open-Meteo called with `timezone=America/New_York` returns exactly 24 rows even
   on DST days — it normalises the discontinuity away. All weather pulls therefore
   use `timezone=UTC` and convert in Silver.

---

## D-04 Feature time convention: absolute offsets from the cutoff

**Decision.** Every lag is expressed as an absolute offset from cutoff `C`, never as
"N days ago". Every feature row carries a `source_timestamp_utc`, and a test asserts
`max(source_timestamp_utc) <= cutoff_utc` across the whole feature table.

**Reason.** "Yesterday" is ambiguous and that ambiguity is where leakage enters.
Anchored on forecast date D, `demand(D-1, h)` is fully available. Anchored on target
date D+1, "yesterday" is D, which is only available through 09:00. README section 7
uses the D-anchored form and is correct — but correctness by convention is not
verifiable, so section 31's "passes a leakage audit" becomes a mechanical test
rather than a review step.

Corollary: `demand(target - 24h)` is available only for target hours 00:00-10:00 of
D+1, so it is **not** a valid generic feature. `target - 48h`, `-168h`, `-336h` are
always safe.

---

## D-05 `hours_ahead` is an evaluation dimension, not a feature

Under a fixed 10:00 cutoff, `hours_ahead` is a deterministic function of the target
hour, so it is collinear with `hour` and adds no information. It stays in
`forecast_accuracy` for slicing (README section 22) and out of the feature list.

---

## D-06 Retrain cadence: monthly refit, daily prediction

README section 21 specifies rolling-origin backtesting but not how often the model
refits. One fit per forecast day over ~2,000 days is ~2,000 fits for no benefit.

**Decision.** Refit on the first of each month using all data up to the prior month
end; predict every day within the month. `forecast_run.training_data_end_timestamp`
records the boundary, so any prediction can be traced to the exact fit that produced it.

---

## D-07 Hard bounds stay explicit alongside statistical detection

README section 16 reduces the range check to `demand > 0`, which no longer catches a
10x unit error or a zero-spell. Both layers are kept: fixed hard bounds calibrated
from observed history (`validate_sources.py` Q6), plus statistical anomaly detection
for unusual-but-possible values. Legitimate extremes must survive — that is what the
extreme-regime evaluation is measuring.

---

## D-08 Weather geography in V1: 8 zone points, unaggregated

PJM spans ~13 states, so a single coordinate is not representative. V1 pulls 8 points
(one per major load zone, see `PJM_POINTS`) and feeds them to the model as separate
features rather than pre-averaging.

**Reason.** Population weighting requires weights that must themselves be justified.
Letting the model learn the relative importance is cheaper and avoids inventing
weights; the population-weighted aggregate moves to V2, where it becomes an
interpretability feature for the dashboard rather than a modelling necessity.

---

## D-09 Feature code lives in exactly one place

README section 29 puts `features.py` under both `src/gold/` and `src/ml/`. Feature
generation is the leakage-critical component, so it lives only in `src/features/`.
Gold and ML both import from there.

---

## D-10 Secrets

`EIA_API_KEY` is read from the environment locally and from a Databricks secret scope
in the workspace. It is never committed and never inlined in a notebook.

---

## D-11 One premium workspace, two catalogs — not Free Edition for dev

Verified 2026-09-08 against subscription `Azure for Students`:

| Component | State |
|---|---|
| `stlakeobs0803` | StorageV2, **HNS enabled** — genuine ADLS Gen2 |
| container `lakeobs` | exists |
| `dbw-lakeobs` | SKU **premium** — Unity Catalog + external locations available |
| `ac-lakeobs` | SystemAssigned MI, holds Storage Blob Data Contributor on the account |
| Spend, last 30 days | $0.00 |

**Decision.** A single catalog `energy` inside the existing premium workspace, with
dev/prod separated by Databricks Asset Bundles targets rather than by catalog.

```
dbw-lakeobs (premium)
└── metastore
    ├── lakeobs    observability project, untouched
    └── energy     this project - bronze / silver / gold / ml
```

The earlier plan of `energy_dev` + `energy_prod` catalogs is withdrawn. The existing
job named `[dev 20133050] lakeobs-medallion` carries the Asset Bundles dev-mode prefix,
so that convention is already established in this workspace; a second, different
dev/prod mechanism would be inconsistent for no gain.

**Reason.** Databricks Free Edition is serverless-only on Databricks-hosted storage
with its own metastore, and cannot attach `stlakeobs0803`. The core of this project —
`abfss://lakeobs@stlakeobs0803.../landing` into Auto Loader into Bronze — is exactly
the part that cannot run there, so dev would exercise a different substrate from prod
and every ingestion line would be rewritten on promotion. Catalog-level separation is
also what UC is designed for and demonstrates governance that two disconnected
workspaces cannot.

Free Edition is kept only as a scratchpad for PySpark syntax, off the credit meter.

---

## D-12 Budget guardrails

Azure for Students is $100 with a hard spending cap. The cap prevents overspend but
stops the project if hit, so the constraint is real.

| Rule | Reason |
|---|---|
| **No SQL Warehouse** | Serverless 2X-Small is ~4 DBU/h. One night left running can consume a third of the credit. |
| Power BI in **Import mode** against ADLS Parquet/Delta | Satisfies README section 26 (dashboard consumes Gold) at zero Databricks compute. |
| Single node, auto-terminate 10 min, autoscale off | The full dataset is under 1M rows; more than one node is never justified. |
| Budget alert at 50% / 80% | Early warning before the cap. |

At roughly $0.5-0.7/hour for a small single-node cluster, $100 buys ~150-200 cluster
hours — ample, provided no warehouse is ever started.

---

## D-13 EIA `period` is UTC — established by measurement

The API response carries no timezone field and the period string has no offset
(`"period": "2026-09-01T00"`), so this had to be determined from evidence rather
than assumed. Mean load profile, PJM, five summer weekdays 2026-07-06..10:

```
  peak   = 22:00  (134,848 MWh)
  trough = 09:00  ( 94,820 MWh)
```

PJM summer load peaks ~18:00 and troughs ~05:00 Eastern Prevailing Time. EDT is
UTC-4, and the returned index sits exactly four hours later at both ends.

**Conclusion.** `period` is UTC. README section 18's choice of
`event_timestamp_utc` as the canonical key needs no conversion at ingest, and local
time is derived in Silver.

Worth keeping in the write-up: the same test is how you would catch an upstream
timezone change, and it costs one query.

---

## D-14 Workspace state as of 2026-09-08

Unity Catalog plumbing for Milestone 1 already exists and needs no work:

| Object | Value |
|---|---|
| Metastore | `32b713ea-...`, default catalog `dbw_lakeobs` |
| Storage credential | `cred-lakeobs` -> access connector `ac-lakeobs` |
| External location | `loc-lakeobs` -> `abfss://lakeobs@stlakeobs0803.dfs.core.windows.net/` |
| Catalogs | `dbw_lakeobs`, `lakeobs` |
| Clusters | none |
| Cluster policies | built-ins only, no custom policy |
| Entitlements | `allow-cluster-create`, `allow-instance-pool-create` |

**Open cost item.** A serverless SQL warehouse `lakeobs-wh` exists: 2X-Small,
`auto_stop = 5 min`, currently STOPPED. Stopped and at minimum auto-stop it is not
burning credit, but it remains the only object in the workspace capable of consuming
the budget quickly — a Power BI DirectQuery connection or a scheduled dashboard
refresh restarts it on every poll. Since D-12 routes Power BI through Import mode
against ADLS, the warehouse has no role in V1. Pending a decision to delete it.

---

## D-15 Auto Loader uses directory listing, not file notifications

Creating `loc-energy` produced a partial test result: Read, List, Write, Delete, Path
Exists and Hierarchical Namespace all passed; **File Events Resource Provision** and
**Teardown** failed with `403 AuthorizationFailure` on `queue.create`.

File events back Auto Loader's *file notification* mode (Event Grid + Storage Queue)
as an alternative to directory listing. Databricks labels them "optional but
recommended". Enabling them requires three roles the access connector does not have:

| Role | Status |
|---|---|
| Storage Blob Data Contributor | already granted |
| **Storage Account Contributor** | missing — *control-plane* on the account |
| EventGrid EventSubscription Contributor | missing |
| Storage Queue Data Contributor | missing |

**Decision.** Force-create the external location and run Auto Loader in directory
listing mode. `cloudFiles.useNotifications` is set to `false` explicitly rather than
left defaulted, so the choice is visible in the code.

**Reason.** File notification mode addresses millions of files and high arrival rates.
This landing zone holds ~2,800 files and grows by a handful per hour, where listing
cost is cents. Buying that optimization would mean granting **Storage Account
Contributor** — a control-plane role that permits managing the account and reading its
access keys — to enable something the workload does not need. Over-privileging an
identity for an unnecessary optimization is the worse trade.

The landing layout helps here: `date=YYYY-MM-DD` sorts lexicographically, so newly
arrived files always sort after already-processed ones and Auto Loader's incremental
listing does not rescan the tree.

**Reversible.** If file notifications are ever wanted:

```bash
SA=$(az storage account show -n stlakeobs0803 -g rg-lakeobs --query id -o tsv)
MI=bafa2676-9cd9-46c2-b60e-f66ddc1dde10
for R in "Storage Account Contributor" "Storage Queue Data Contributor" \
         "EventGrid EventSubscription Contributor"; do
  az role assignment create --assignee "$MI" --role "$R" --scope "$SA"
done
```

---

## D-16 Setup as built (2026-09-08)

| Object | Value |
|---|---|
| Container | `energy` in `stlakeobs0803` |
| Storage credential | `cred-lakeobs` (reused) |
| External location | `loc-energy` -> `abfss://energy@stlakeobs0803.dfs.core.windows.net/` |
| Catalog | `energy`, storage_root `abfss://energy@.../_managed` |
| Schemas | `bronze`, `silver`, `gold`, `ml` |
| Cluster policy | `energy-single-node`, id `00094924DD2F31E7` |

No new Azure resources beyond the container: the access connector's *Storage Blob Data
Contributor* sits at account scope and covers it.

The catalog was created once at the container root, then dropped and recreated with the
`_managed` suffix while still empty. UC does not allow `storage_root` to change after
creation, so the same correction after tables existed would have meant relocating data.
Resulting layout matches the `lakeobs` convention:

```
abfss://energy@stlakeobs0803.dfs.core.windows.net/
├── _managed/       UC managed tables
├── _checkpoints/   Auto Loader state (created on first Bronze run)
└── landing/        raw files from the extractors
```

`landing/` rather than `raw/` deliberately: README section 12 already defines Bronze as
a *Delta table*, so naming the file path `raw` would blur the two. Three distinct
things, three distinct names.

---

## D-17 Predictive Optimization disabled on `energy`

```
energy          enable_predictive_optimization = DISABLE
energy.bronze   INHERIT   -> resolves to DISABLE
energy.silver   INHERIT   -> resolves to DISABLE
energy.gold     INHERIT   -> resolves to DISABLE
energy.ml       INHERIT   -> resolves to DISABLE
```

Set through the Unity Catalog API, so no compute had to start:

```bash
databricks catalogs update energy --enable-predictive-optimization DISABLE --profile lakeobs
```

**Reason.** Predictive Optimization runs `OPTIMIZE` and `VACUUM` on managed tables
automatically, on **serverless compute that bills**. It earns its cost on large,
frequently-rewritten tables. This project's largest table is ~270k rows across ~34 MB,
where background compaction would cost more than the scans it accelerates.

The catalog was set before any table existed, so no table has ever been enrolled.

**Inheritance.** account -> metastore -> catalog -> schema -> table, where a child
inherits unless it sets its own value. The schemas reading `INHERIT` are therefore
correct rather than incomplete — they resolve to the catalog's `DISABLE`. A single
table can still opt back in later with
`ALTER TABLE ... ENABLE PREDICTIVE OPTIMIZATION` if one ever grows enough to warrant it.

**The trade this accepts.** With PO off, compaction and file cleanup are now manual.
Small-file accumulation and unreclaimed tombstones are real over a long enough horizon,
so one `OPTIMIZE` plus `VACUUM` pass near the end of V1 is worth running deliberately.
At this data size that is sufficient; the point is that the cost is correctly priced,
not that it is zero.

**Note.** The `lakeobs` catalog was already set to `DISABLE`, so this matches the
convention already in place in the workspace. It was read for reference and not modified.

---

## D-18 Data quality is two layers: quarantine for the impossible, a flag for the merely suspect

Three anomaly detectors were built and rejected against the real 2019-2026 backfill
before the fourth was kept. Recording the failures because each one looks correct until
it meets the data.

| Detector | Why it failed |
|---|---|
| Deviation from interpolated neighbours | One bad value poisons the interpolation of both adjacent hours, so it flags three rows per genuine fault. At 2019-12-11 the valid 99,567 and 99,613 either side of the bad 417,669 both scored above 1.5. |
| Ratio to a centred rolling median | Immune to contagion but fires on steep ramp hours: a valid 138,575 MWh at 13:00 scored 1.34, inside any useful threshold. |
| Same clock hour +/-24h via `greatest`/`least` | `greatest` and `least` skip NULLs, so where one neighbour is absent the other decides alone — and the spike's own +/-24h neighbours get flagged as *low* anomalies. Contagion again, one day out instead of one hour. |
| **Same clock hour +/-24h, both sides required** | Kept. Comparing like clock hour with like removes the ramp problem; requiring both neighbours to exist *and* to disagree independently removes the contagion, because a spike corrupts only one of the two comparisons and the other vetoes. |

Flag count on real data fell from 31 (rolling median) to **9** with no genuine fault lost.

### The layer boundary

| Layer | Action | Catches |
|---|---|---|
| Hard bound, per type | **quarantine** — row leaves Silver | Values that cannot be demand: 1.5e9, 2.1e9, 4.3e8, and 417,669 |
| Same-hour comparison | **flag** — row stays, boolean column | Plausible magnitudes that are still wrong: 192,229 to 262,651, and one low outlier at 56,260 |

Layer 2 only flags, so a false positive at a ramp hour costs nothing and README section
16's rule — *do not reject legitimate extreme demand solely because it is unusual* — is
honoured structurally rather than by choosing a lucky threshold. The model and the Gold
peak tables each decide for themselves whether to exclude flagged rows.

Bounds are per type because interchange is signed: a single blanket `value >= 0` would
have quarantined **1,627 valid negative TI readings**.

Upper bound for D and DF is 300,000 MWh — roughly 1.8x PJM's all-time peak of ~165,563
MW, standing since 2006. Loose enough that it can never reject a genuine record, which
is the entire job of a hard bound.

---

## D-19 The EIA feed itself loses demand data on DST transition days

Measured across the full Silver output:

```
ordinary days   67,011 hours    34 NULL demand     0.051%
DST days           359 hours    87 NULL demand    24.234%    <- 475x higher
```

Four of the five worst days in seven years are DST transitions:

```
2020-03-08   23 expected   21 of 23 hours missing demand
2022-03-13   23 expected   21 of 23 missing
2023-11-05   25 expected   24 of 25 missing
2024-03-10   23 expected   21 of 23 missing
2020-08-03   24 expected   23 of 24 missing    <- unrelated feed outage
```

Not every transition is affected — 2019, 2021, 2023-spring, 2025 and 2026 are intact —
so it is intermittent rather than systematic.

**Why this matters beyond curiosity.** It is upstream confirmation that DST is the
weak point in this data, not a theoretical concern. It also has a modelling
consequence: on those dates the demand lag features are unavailable, so the day-ahead
feature builder must tolerate NULL lags around transitions rather than assume a
complete history. That is a Milestone 4 concern, recorded here so it is not discovered
by a silent accuracy drop.

---

## D-20 Silver output as built

```
bronze rows          271,989
silver rows           67,375     gap-free hourly spine, 2019-01-01T00 -> 2026-09-08T06
quarantine rows             7     all IMPOSSIBLE_VALUE
anomaly flagged             9
all-metric-missing hours   92
revisions detected          0     the 2,976 duplicate pairs are byte-identical
tests                      33     passing
```

Completeness matched the calendar on **all 15 DST days** in range, 23 or 25 hours as
appropriate.

One known gap: the trailing local day is partial by construction — on 2026-09-08 the
feed reached 06:00 UTC, so the local day holds 3 of 24 hours. The completeness report
excludes the final local date rather than reporting a false shortfall.

---

## D-21 Weather feature set trades variables for history

Measured 2026-09-08 by binary search over the Previous Runs archive, per variable:

| Variable | First available | Span to today |
|---|---|---|
| `temperature_2m` | **2021-03-24** | ~1,994 days |
| `relative_humidity_2m` | 2024-01-19 | ~963 days |
| `dew_point_2m` | 2024-01-19 | ~963 days |
| `wind_speed_10m` | 2024-01-19 | ~963 days |
| `precipitation` | 2024-01-19 | ~963 days |
| `apparent_temperature` | 2024-01-19 | ~963 days |

Temperature reaches back roughly **twice as far** as everything else. README section 7
lists humidity, wind and precipitation forecasts as features; taking them costs half
the training history.

**Decision.** The primary model uses temperature only, via CDD/HDD, over the full
2021-03-24 window. The richer set becomes an ablation on the shorter window.

**Reason.** Temperature is the dominant weather driver of electricity load. Humidity
acts on load second-hand through heat index, wind affects the supply side far more than
demand, and precipitation barely registers. Trading a weak marginal signal for a
doubling of training data is the right direction; 963 day-ahead runs is thin for a model
carrying tens of features.

All six variables are ingested across the whole range regardless. The API returns NULL
before each variable's floor, storage is trivial, and re-ingesting later to enable the
ablation would be waste.

### Both forecast vintages are ingested, and why

`forecast_lead_days` is the authoritative provenance column.

| Lead | Run origin | Status for a 10:00 ET cutoff on day D |
|---|---|---|
| 2 | day D-1 | **Provably** earlier than the cutoff. Safe primary. |
| 1 | day D | Very likely earlier, but Open-Meteo does not expose the archived run hour, so it cannot be proven. |

Rather than assume lead 1 is safe or discard it, both are stored and the model reports
against each. Measured on July 2024 at Philadelphia, lead 1 and lead 2 differ by
**MAE 1.833 °C** (correlation 0.848) — genuinely distinct vintages, not a duplicated
series. That temperature gap propagates into a demand-forecast gap, which makes the
price of the safety margin a measured number instead of an assumption.

This also means README section 15's `forecast_run_timestamp_utc` cannot be populated
honestly: the archive gives "N days earlier", not a run instant. `forecast_lead_days`
replaces it rather than fabricating a timestamp.

---

## D-22 Data quality needs a third, absolute layer

Layer 2 compares each hour with the same clock hour a day either side. It is a
*relative* test, so a reading whose neighbours are also inflated escapes it. Running
Gold over real history exposed one survivor:

```
2020-07-29T21   176,085 MWh   ratio to neighbours 1.26   (threshold 1.30)
```

176,085 exceeds PJM's all-time peak of ~165,563 MW by 6%, in a month that already
contained three confirmed corrupt readings. It published as the all-time peak.

**Layer 3.** `is_above_historical_record`, an absolute ceiling at 165,563 MWh. It flags
rather than rejects, so a genuine future record surfaces for review instead of being
thrown away — 2026 peaks already reach 162,648, so it will begin firing on real records,
which is the intended behaviour.

| Layer | Kind | Action | Catches |
|---|---|---|---|
| 1 hard bound | absolute, wide | quarantine | Cannot be demand: 1.5e9, 2.1e9, 417,669 |
| 2 same-hour +/-24h | relative | flag | Isolated spikes: 192,229 to 262,651, one low at 56,260 |
| 3 historical ceiling | absolute, tight | flag | Inflated readings with inflated neighbours: 176,085 |

Relative and absolute tests fail in different directions, which is why neither alone
was enough. Layer 1 must stay wide so it never false-positives; layer 3 can be tight
precisely because it only flags.

### Effect on published peaks

| Rank | Unfiltered | Filtered |
|---|---|---|
| 1 | 2020-07-24  262,651 | 2026-07-02  162,648 |
| 2 | 2020-09-03  250,825 | 2026-07-01  161,965 |
| 3 | 2020-07-27  245,799 | 2025-06-23  160,560 |
| 4 | 2020-07-13  224,345 | 2026-07-15  159,046 |
| 5 | 2020-04-10  215,682 | 2025-06-24  158,646 |

Every filtered peak lands at hour 18 local, PJM's expected summer peak hour, and all sit
below the 2006 record. Annual peaks now read 152,315 / 145,428 / 149,590 / 148,528 /
147,605 / 153,121 / 160,560 / 162,648 for 2019-2026: a COVID trough in 2020 and steep
2025-26 growth consistent with PJM's known data-centre load. The series validating
itself against outside knowledge is worth more than any internal check.

---

## D-23 ~~The benchmark is measured, and peak timing is where the opportunity is~~ SUPERSEDED BY D-28

EIA's published day-ahead forecast (`DF`) scored against actual demand over 2,782
complete operating days:

```
peak magnitude bias      -0.345 %     slight under-forecast
peak magnitude MAPE       1.849 %
peak timing MAE           1.711 hours
peak timing hit rate      13.7 %      exact hour
```

This is the bar the challenger has to clear, and it is now a number rather than an
assumption.

The 13.7% exact-hour hit rate is the interesting one. A professional operator's
day-ahead forecast identifies the peak *hour* correctly barely one day in seven, while
placing peak *magnitude* within 1.85%. Magnitude is close to solved and timing is not,
so peak timing is where a challenger has room to add value — and it is also the metric
a non-technical reader immediately understands.

Reporting plan follows from this: magnitude MAPE for completeness, timing MAE and hit
rate as the headline.

> **Superseded.** Every number above was computed on misaligned series. `DF` is
> labelled an hour earlier than `D`, so the 13.7% hit rate is an artefact and the
> conclusion drawn from it is backwards. See D-28 for the corrected figures.

---

## D-24 Publication lag is per metric, and it invalidates the obvious feature

Measured 2026-09-08 by comparing each ingestion run's `ingested_at` with the newest
`period` it returned:

| Metric | Lag | Used |
|---|---|---|
| `D` demand | 1.41 h | 3.0 h |
| `DF` day-ahead forecast | 3.41 h | 5.0 h |
| `NG` net generation | 28.41 h | 30.0 h |
| `TI` interchange | 28.41 h | 30.0 h |

**Consequence.** `demand_cutoff_minus_1h` — the most natural feature imaginable — does
not exist. At a 10:00 cutoff the 09:00 reading has not been published. Cutoff-anchored
demand offsets therefore start at 3h.

`NG` and `TI` lag by more than a day, so even a target-anchored `t-48h` is unavailable
for late target hours. They are ingested and held in Silver but **excluded from the V1
feature set**: their staleness leaves them nearly useless for a day-ahead horizon, and
net generation is close to collinear with demand anyway.

The values used carry margin over the measurements because one observation is not a
distribution. `PUBLICATION_LAG_HOURS` is the single place to change it.

---

## D-25 The leakage audit is mechanical, and it is tested from both directions

README section 31 lists "feature generation passes a leakage audit" in the definition of
done. A review step cannot discharge that, because leakage is invisible by construction.

Each feature declares its provenance as a `FeatureSpec`: an anchor (cutoff, target,
calendar or weather run), an offset, and a metric whose publication lag applies.
`audit()` computes when the information became *knowable* and compares that with the
cutoff. Only calendar features may claim "always knowable".

The join logic in `features/build.py` is generated from the same registry, so the
implementation cannot drift from the audited declaration, and
`test_features_build.py` asserts every declared feature exists as a column.

### Result on the real table

```
pairs checked   47,832        (1,993 forecast dates x 23/24/25 target hours)
violations           0
horizon range     14.0h .. 38.0h    exactly as declared
```

### The audit is shown to have teeth

An audit that only ever passes is indistinguishable from no audit. Five features that
each look defensible are kept in `KNOWN_LEAKY`, and a test asserts every one is caught:

| Feature | Why it leaks |
|---|---|
| `demand_cutoff_minus_1h` | 09:00 demand does not exist at a 10:00 cutoff (D lag ~1.4h) |
| `demand_same_hour_minus_1d` | unavailable once the horizon passes ~21h |
| `actual_demand_at_target` | the label itself |
| `net_generation_same_hour_minus_2d` | NG lag ~28h, so even t-48h is unpublished |
| `temp_c_region_mean_lead0` | observed weather at the target hour — hindsight |

A further test pins the boundary: target-anchored `t-24h` is safe for horizons up to
~21h and leaks beyond, which is precisely why 48h is the smallest safe target offset.

And `lead 1` weather **fails** the strict audit, by design. Open-Meteo does not publish
the archived run hour, so a run from day D cannot be proven to precede a 10:00 cutoff on
day D. Rather than assume it, lead 2 is the primary and lead 1 is a reported sensitivity
variant. The audit flagging the one thing already known to be uncertain is evidence it
is measuring something.

---

## D-26 Spark's null-skipping aggregates, three times in one sitting

The same hazard produced three distinct bugs while building Silver, Gold and features.
Recording it as one pattern because the next instance will not look like the last.

| Where | Expression | What went wrong |
|---|---|---|
| Anomaly detector | `greatest(prev, next)` | With one neighbour NULL the other decided alone, flagging a spike's own +/-24h neighbours as low anomalies |
| Degree days | `greatest(t - base, 0.0)` | `greatest(NULL, 0.0)` is **0.0**, so a missing temperature became "zero cooling degrees" — a fabricated mild hour for the model to learn |
| Daily aggregates | `sum(cdd)` | A day missing hours reported a smaller total, reading as a milder day rather than an incomplete one |

`greatest`, `least` and `sum` all ignore NULLs, so absence silently becomes a plausible
value rather than an error. Each fix is the same shape: guard with an explicit
`isNotNull()` or a completeness condition so absence stays absent.

Degree-day coverage was what exposed the second one — 99.99% against temperature's
98.91%, when a derived column cannot possibly out-cover its source. Cross-checking
coverage between a column and its input is now the routine check; after the fix both
read 98.91%, and the daily aggregates 98.85%.

---

## D-27 Feature table as built

```
feature rows        47,832      2021-03-24 -> 2026-09-06
columns                 42
declared features       33      Model B (18 of them Model A: no weather)
leakage violations       0      over all 47,832 pairs
tests                   96      passing
```

Coverage: demand 99.85%, calendar 100%, weather 98.91%, daily weather 98.85%,
label 99.85%, EIA `DF` benchmark 99.70%.

Sanity check on the hottest day in range, forecast 2026-07-01 for 18:00 local on 07-02:

```
temp_c_region_mean_lead2      36.92 C      cdd_region_lead2        18.59
temp_c_target_day_max_lead2   37.90 C      cdd_target_day_sum      316.16
label_demand_mwh             162,648       benchmark_eia_df    164,450  (+1.11%)
hours_ahead                    32.0
```

The highest demand in the dataset coincides with the highest forecast temperature, and
the BA's own forecast lands within 1.11%. Independent agreement of that kind is worth
more than any internal assertion.

---

## D-28 `DF` and `D` use different hour labels, and it inverted the previous conclusion

Found by reading sample rows rather than by any test. The Gold peak table showed
`peak_timing_error_hours = -1` on nine of ten consecutive days; across all 2,782
complete days it is -1 on **62.4%** of them and 0 on only 13.7%, with the bias present
in every single year from 2019 to 2026.

The decisive check was on the whole series rather than the peak, over 67,096 paired
hours:

```
DF(t) vs D(t-2)   MAE 7,192.0
DF(t) vs D(t-1)   MAE 5,247.5   corr 0.92199
DF(t) vs D(t+0)   MAE 3,320.5   corr 0.96973
DF(t) vs D(t+1)   MAE 2,224.8   corr 0.98732   <- best
```

No physical mechanism makes a *forecast* better at the hour after the one it aims at.
The two series carry different hour-labelling conventions.

Corroborated from outside the data: published day-ahead forecasts for large balancing
authorities are known to achieve roughly 1.5-3% hourly MAPE. The misaligned figure of
3.608% sits outside that band; the aligned 2.435% sits inside it.

### Corrected benchmark

| Metric | Misaligned | **Aligned** |
|---|---|---|
| hourly MAE | 3,320.5 MWh | **2,224.8 MWh** |
| hourly MAPE | 3.608 % | **2.435 %** |
| hourly RMSE | 4,184.2 MWh | **2,854.6 MWh** |
| peak magnitude bias | -0.345 % | -0.337 % |
| peak magnitude MAPE | 1.849 % | 1.845 % |
| peak timing MAE | 1.711 h | **1.106 h** |
| peak timing hit rate | 13.7 % | **62.5 %** |

Peak magnitude barely moves, because comparing one day's maximum with another's is
insensitive to a one-hour shift. Everything hour-resolved moves a great deal.

### Why this mattered more than it looks

The bar is substantially higher than reported: hourly MAPE 2.435% rather than 3.608%,
and the benchmark already identifies the peak hour 62.5% of the time rather than 13.7%.

D-23's conclusion — *magnitude is nearly solved, timing is where the opportunity is* —
is therefore wrong, and dangerously so. A challenger built against the misaligned
benchmark could have "beaten" it by shifting its output an hour: a 4.6x apparent
improvement in timing hit rate, from beating an artefact. That result would have been
the headline of the write-up and would not have survived one question from anyone who
knew the domain.

### Fix

`align_day_ahead_forecast` in Silver moves the published forecast onto the demand
timeline and keeps `day_ahead_forecast_as_published_mwh` alongside for traceability.
It runs after the spine, because `lag` counts rows and only a gap-free spine makes one
row equal one hour — a test asserts that dependency.

### Open

This establishes the offset between the two series, not which one is absolutely
correct. If `D` is itself the shifted series, the weather join inherits the same
one-hour error. Thermal inertia delays load response to temperature by 1-3 hours
anyway, so correlation cannot separate convention from physics here. M5 therefore
treats the weather offset as a modelling choice — fit at 0, -1 and -2 hours and let the
backtest decide — rather than asserting an answer.

### Method note

96 passing tests did not catch this, and could not have: every test was written against
the same assumption that the two series shared a timeline. What caught it was looking
at ten rows of output and noticing a column that was -1 too often. Worth keeping as a
habit rather than a one-off.

---

## D-29 M5: the challenger does not beat the balancing authority

Rolling-origin, 55 monthly folds, 2022-03 → 2026-09, 39,476 scoreable hours per model.
Every model scored on identical rows.

| Model | MAE MWh | RMSE | MAPE | bias MWh | vs benchmark |
|---|---|---|---|---|---|
| seasonal naive (lag-168) | 8,300 | 11,410 | 8.560 % | -88 | +261.8 % |
| **EIA DF (benchmark)** | **2,294** | **2,934** | **2.475 %** | -1,314 | — |
| xgb_a (no weather) | 5,203 | 7,211 | 5.299 % | -404 | +126.8 % |
| xgb_b (+ weather) | 2,581 | 3,545 | 2.678 % | -1,020 | **+12.5 %** |

Peak: benchmark hits the exact peak hour 64.7% of days against xgb_b's 48.4%. Extreme
regime: benchmark 2.227% MAPE against xgb_b's 3.375%.

**The independent challenger loses on every dimension measured.** Recording it plainly
because the alternative — quietly reframing until something looks like a win — is what
the whole leakage and alignment apparatus in this project exists to prevent.

The benchmark's 2.475% here matches the 2.435% measured independently over the full
period in D-28, which is the evidence that the harness is scoring correctly rather than
penalising the challenger.

### What weather is worth — the D-02 ablation, answered

Same window, same folds, same rows; only the feature set differs.

```
xgb_a   18 features, no weather    MAE 5,203   MAPE 5.299%
xgb_b   33 features, + weather     MAE 2,581   MAPE 2.678%
                                   -> MAE -50.4%, MAPE -49.5%
```

Larger still in the extreme regime, 9.029% → 3.375% MAPE, which is what one would
expect if peaks are weather-driven. This is the clean result of the project.

### Why it loses: concept drift, diagnosed then tested

```
PJM demand      2022 mean 90,865 MWh  ->  2026 mean 101,336    +11.5%
xgb_b bias      2022 -497            ->  2026 -1,998
corr(training rows, bias) = -0.585
```

The benchmark is also biased low (-1,181 → -1,582) but **flat**; the challenger
**drifts**. An expanding window drags 2021-2023 levels into a 2026 prediction, and PJM
grew through the period on data-centre load — the same growth visible in the annual
peaks of D-22.

Notably the challenger was *less* biased than the benchmark in 2022-2023 (-498 and -303
against -1,181 and -1,050). Drift is what kills it, not modelling capacity.

### Two pre-registered fixes, both negative

Named before the numbers were seen, each aimed at the diagnosed mechanism.

| Variant | MAE | MAPE | bias | 2026 bias |
|---|---|---|---|---|
| xgb_b | 2,581 | 2.678 % | -1,020 | -1,998 |
| xgb_b_slide24 | 2,598 | 2.696 % | -765 | -1,240 |
| xgb_b_ratio | 2,663 | 2.742 % | -799 | -1,602 |
| xgb_b_ratio_slide24 | 2,718 | 2.795 % | -536 | -997 |

Both **worked on what they targeted** — bias falls monotonically, 2026 bias halves —
and both **made accuracy worse**. A bias-variance trade: the sliding window buys
recency with less data, and the ratio target divides by a baseline that carries its own
noise. Plain `xgb_b` remains the best challenger.

The list stopped here as committed. With 55 folds, sweeping until something wins fits
the backtest rather than the problem.

### A flaw in the comparison, stated

The extreme-regime slice is **not** comparable between expanding and sliding variants.
The threshold is the 95th percentile of each fold's *training* labels, so a sliding
window sees different training data, sets a different threshold, and classifies a
different row set as extreme — 2,521 rows against 2,701. The overall and peak
comparisons are unaffected; only the regime slice is.

### The residual gap is not all drift

MAPE by year shows a stable ~0.2pp deficit even in 2023, when the challenger was barely
biased. Plausible causes, none of them fixable by tuning:

* the benchmark uses fresh weather; this model uses the **lead-2** vintage, deliberately,
  because lead 1 cannot be proven to precede the cutoff (D-21). That safety margin has a
  price which has not yet been measured.
* PJM knows scheduled generator outages, large-customer notifications and its own
  operating plan. None of that is in EIA-930.
* the benchmark is re-forecast daily; this model refits monthly (D-06).

### What this is worth saying out loud

"Built an independent day-ahead forecast that lands within 12.5% of the balancing
authority's own, and measured that weather features account for half its accuracy" is a
defensible claim. "Beat the operator by 48%" would not have survived one question, and
after D-28 it is clear how easily that number could have been manufactured.

---

## D-30 The safety margin costs 1.01% MAE — the D-21 question, answered

The primary model uses **lead-2** weather because a run from D-1 is provably earlier
than a 10:00 cutoff on day D. Lead 1 is a run from day D itself, very likely earlier
but not demonstrably so, since Open-Meteo does not publish the archived run hour.
D-21 deferred the cost of that caution to measurement. Measured:

| Model | MAE MWh | MAPE | bias | gap to benchmark |
|---|---|---|---|---|
| EIA DF (benchmark) | 2,294 | 2.475 % | -1,314 | — |
| xgb_b_lead1 — **audit rejects** | 2,555 | 2.653 % | -865 | +11.36 % |
| xgb_b lead-2 — deployable | 2,581 | 2.678 % | -1,020 | +12.49 % |

```
price of provable availability = +1.01% MAE
```

**The leakage discipline is nearly free.** One point of the 12.5% gap comes from using
weather that can be proven available; the other eleven do not.

That is worth more than a win would have been. Before this the honest position was "the
model is 12.5% behind, and some unknown share of that is self-imposed caution" — an
argument with a hole in it. Now the caution is priced, and the hole is closed: the gap
is not about leakage avoidance.

Peak timing improves slightly on lead 1 (50.5% against 48.4% exact-hour) and the extreme
regime is unchanged within noise (3.353% against 3.375% MAPE), so fresher weather is not
where the remaining distance lies either.

### What the remaining ~11.4% is, then

Not measurable from this data, and not fixable by tuning:

* PJM knows its own scheduled generator outages, large-customer notifications and
  operating plan. None of that appears in EIA-930.
* The benchmark is re-forecast daily against current conditions; this model refits
  monthly (D-06).
* PJM's weather input is an ensemble over many more stations than eight points.

A challenger built on public data alone landing within 12.5% of that is the result, and
it is a better claim than a manufactured win.

### The audit gate ran before the numbers

`scripts/run_lead1_sensitivity.py` asserts the lead-2 set passes and the lead-1 set is
**rejected** before computing anything, printing the specific violation:

```
temp_c_chicago_comed_lead1: knowable at 2024-06-21T04:00:00+00:00,
  +14.00h relative to cutoff 2024-06-20T14:00:00+00:00
```

If lead 1 ever passed, the script stops without printing a metric. The failure mode this
guards against is ordinary: a variant is run "just to measure", it looks better, and
weeks later it has quietly become the production model because nobody remembers why it
was excluded. Gating on the audit first makes that promotion a deliberate act.

---

## D-31 M6: forecast tables, the Elliott stress test, and what it changed

### Three tables, not one

`forecast_run` (6,604) / `forecast_prediction` (158,492) / `forecast_accuracy`
(158,061). The split is what lets a prediction be traced to the fit that produced it —
cutoff, training-data end, model version. Flattened into one wide table you can see
what was predicted but not what the model knew, and "why was this day bad" stops being
answerable.

A defect found while building them: `cutoff_utc` was missing from the backtest's carried
columns, and a forecast run is identified by its cutoff rather than its date — 10:00
local is 14:00 UTC in EDT and 15:00 in EST. Fixed, and the carry list now skips absent
columns so a minimal test frame need not supply every one.

### Winter Storm Elliott, 23-25 December 2022

Full write-up in [`case-study-elliott.md`](case-study-elliott.md). Three findings that
change how results should be reported.

**1. Extreme-regime error is one-sided, not just larger.**

| Model | event MAE | event bias | backtest MAE | degradation |
|---|---|---|---|---|
| EIA DF | 5,255 | -2,377 | 2,294 | 2.3× |
| xgb_b | 13,089 | **-13,089** | 2,581 | 5.1× |
| xgb_a | 24,610 | **-24,348** | 5,203 | 4.7× |

For every challenger **MAE equals |bias| exactly**: all 72 event hours under-forecast,
without one exception. The benchmark was wrong in both directions — noisy, not blind.
This is a squared-error objective regressing to the mean precisely where the tail
matters, and it is the concrete case for the asymmetric loss listed under V2.

**2. In severe cold the daily peak moves to the morning, and everyone missed it.**

On 24 and 25 December the peak landed at 09:00 and 08:00 local rather than the evening,
because heating load tracks the overnight temperature minimum instead of the evening
activity cycle. Timing errors of +9 to +13 hours — **including the benchmark's**.

A forecast 2.5% off on magnitude that places the peak twelve hours late has reserves
positioned for the wrong half of the day. The 48.4% exact-hour figure averages over
days whose shape never inverts, so peak timing needs a regime split before it is
reported at all.

**3. Weather held up under stress.** It halved event error, 24,610 → 13,089, consistent
with the 50.4% reduction measured across the whole backtest. The benefit is not an
artefact of ordinary days.

Recovery matters too: by 26 December both the benchmark and xgb_b were back to ordinary
errors. Nothing was structurally broken — the models had simply never seen the regime.

### Power BI

A `.pbix` is a proprietary binary and is not generated here. What is: the tables in the
shape the report consumes, the data model, the DAX, and a layout preview rendered from
`forecast_accuracy.parquet` so the design can be reviewed before anyone opens Desktop.
Recorded as `[~]` in the definition of done rather than `[x]`, because claiming a
deliverable that does not exist is the same error this project spent D-28 correcting.

Two constraints written into the spec: `Bias MWh` sits beside `MAE MWh` everywhere
accuracy appears, and peak-timing hit rate is never shown without a regime split. Both
come directly from Elliott.

---

## D-32 The workspace is serverless-only, which invalidates part of D-12

Creating a job with a classic job cluster failed:

```
Error: Only serverless compute is supported in the workspace.
```

`dbw-lakeobs` accepts **no classic compute at all** — not job clusters, not all-purpose
clusters. `databricks clusters list` returns empty, and the pre-existing
`lakeobs-medallion` job declares no `job_clusters` and no `environments`, which is what
a serverless job looks like.

**Consequences.**

The cluster policy `energy-single-node` built in setup step B1 is **inert**. Every knob
in it — `num_workers`, `autotermination_minutes`, `runtime_engine`, `node_type_id` —
configures a classic cluster, and none can be created here. It is left in place as a
version-controlled artefact for a workspace that does allow them, but it guards nothing
today. Claiming otherwise on the architecture diagram would be a decoration.

The cost model is different, and better:

| | Classic | Serverless |
|---|---|---|
| Billed for idle | yes, until autotermination | **no** — only while executing |
| Startup | 3-5 min, billed | seconds |
| Forgotten-cluster risk | real, the reason for the 10-min cap | **does not exist** |
| Photon toggle | STANDARD vs PHOTON | not exposed |

So the single largest guardrail from D-12 — a hard 10-minute autotermination — was
protecting against a failure mode this workspace cannot have.

**What survives unchanged.** The SQL Warehouse risk is still real: `lakeobs-wh` is
serverless too, and a Power BI DirectQuery connection or a scheduled dashboard refresh
restarts it on every poll. That is why `docs/powerbi.md` specifies Import mode. And
`custom_tags.project = "energy"` moves from the (inert) policy onto the job itself, so
per-project attribution in `system.billing.usage` still works.

**An earlier claim of mine was also wrong.** Recommending against Free Edition, I framed
the contrast as "Free Edition is serverless-only, your Premium workspace has clusters."
The second half is false — this workspace is serverless-only as well. The conclusion is
unaffected, because it never rested on compute type: Free Edition cannot attach
`stlakeobs0803`, and that is the whole of the argument (D-11).

---

## D-33 M1b complete — and local matches cloud exactly

`energy_bronze_ingest`, a serverless job running `01_bronze_autoloader`, 60-94 s per run.

| | Databricks Bronze | local landing |
|---|---|---|
| `eia_region_data` rows | 271,989 | 271,989 |
| distinct source files | 2,839 | 2,839 |
| keys with >1 vintage | 2,976 | 2,976 |
| `weather_forecast` rows | 777,600 | 777,600 |
| distinct source files | 2,025 | 2,025 |
| after revision resolution | 765,696 | 765,696 |

Every figure agrees. That is the evidence for D-12's premise: developing Silver, Gold,
features and models locally at $0 was not a shortcut that happened to work, it produced
the same data the cloud path produces.

One number initially looked wrong — Bronze weather 777,600 against a remembered 765,696
— and the mismatch was mine, not the pipeline's: Bronze holds every vintage while the
765,696 figure is *post*-resolution Silver. The 11,904 difference is the July 2024
validation run, weather's own revision fixture, matching EIA's 2,976 from August.

### Two things that went wrong, both instructive

**A log line failed a task that had already succeeded.** The first run raised
`TypeError: unsupported format string passed to NoneType` on
`f"{p['numInputRows']:,}"` — on serverless, `lastProgress` can return `numInputRows`
unset. `awaitTermination()` had already returned and Delta had already committed, so
the data was fine and the task reported FAILED.

In production that is the dangerous shape: the job alerts, someone retries, and against
a non-idempotent sink the retry double-writes. Here the retry was safe because Auto
Loader's checkpoint already held all 2,839 files — the failure accidentally
demonstrated the property the notebook asserts. Fixed by measuring the row delta from
the table, which is always available and is the number anyone actually wants.

**`az storage fs file list` reported the upload going backwards** — 305, then 198, then
88 — because the listing is paged and unreliable against a tree being written. azcopy's
own `jobs show` had it right at 77.6%. Progress should be read from the writer, not
inferred from the destination.

### Idempotence is asserted, not assumed

```python
added = ingest("eia_region_data", "eia/region-data", partition_cols="respondent,date")
assert added == 0, f"re-run ingested {added:,} rows again — the checkpoint is not holding"
```

Bronze is append-only, so a checkpoint that failed to hold would not raise an error —
it would silently duplicate every row on each run, and Silver would read the duplicates
as a flood of revisions. The metric built to measure upstream revisions would become
the thing hiding the fault.

---

## D-34 The full DAG runs on Databricks, and four bugs only the deployment found

`energy_daily_pipeline`, 7 tasks, **233 s end to end** on serverless.

```
bronze  61s ─ silver 26s ─┬─ gold 17s
                          └─ features 35s ─ leakage_audit 19s ─ forecast 78s ─ accuracy 11s
                                                                        (accuracy also ← silver)
```

Every layer reproduces the local numbers exactly:

| | Databricks | local |
|---|---|---|
| bronze eia / weather | 271,989 / 777,600 | 271,989 / 777,600 |
| silver hourly / quarantine / weather | 67,375 / 7 / 765,696 | 67,375 / 7 / 765,696 |
| gold daily / complete days | 2,809 / 2,782 | 2,809 / 2,782 |
| features rows × cols | 47,832 × 57 | 47,832 × 57 |
| leakage violations | 0 of 47,832 pairs | 0 |

The notebooks import from `src/`, uploaded as workspace files, rather than restating
any logic. A copy would drift from the version 110 tests cover, and the drift would
not announce itself.

The audit is a **gate**, placed upstream of the forecast: a leaking feature set must
stop the pipeline, not produce a number that looks usable.

### Four failures, none of which local testing could have caught

| # | Symptom | Cause |
|---|---|---|
| 1 | `TypeError: unsupported format string passed to NoneType` | `lastProgress['numInputRows']` is unset on Spark Connect — and the write had **already committed**, so a log line failed a task that had succeeded |
| 2 | `NOT_SUPPORTED_WITH_SERVERLESS: PERSIST TABLE` | one `.cache()` in `build_features` |
| 3 | `can't compare offset-naive and offset-aware datetimes` | Spark `collect()` returns naive; the datetimes built here are aware |
| 4 | `audit failed to catch known-leaky features` | the **gate's own self-check** was wrong, not the audit |

The first three share a shape: **the local caller happened to supply the favourable
case**. Tests ran on the engine that has `.cache()`, against a caller that passed aware
datetimes, on a runtime that populates `numInputRows`. No amount of testing on that
substrate reaches them.

Each is now guarded:

* #1 — the row delta is measured from the table, which is always available and is the
  number anyone wants anyway.
* #2 — a test that **reads the source** rather than running it: no `.cache()` or
  `.persist()` anywhere under `src/`. A transform library must run on whatever engine
  the caller has; caching is the caller's decision, and `scripts/` may still do it.
* #3 — `_as_utc()` normalises at the audit boundary. A naive value is read as UTC,
  which is true *because* `spark.sql.session.timeZone` is pinned in both
  `local_session()` and the job's `spark_conf` — the reason for pinning now sits beside
  the code that depends on it.
* #4 — the self-check now asks the right question.

### #4 is a different kind of failure and worth separating

The audit was correct; its guard was not. The self-check took one arbitrary
(cutoff, target) pair and demanded all five known-leaky features be caught there. Two
of them leak only past a horizon threshold — `demand_same_hour_minus_1d` beyond ~21h,
`net_generation_same_hour_minus_2d` beyond ~18h — so at an early target hour they are
genuinely safe. The local test had it right with `any(...)` across all pairs; the
notebook wrote `all` against one.

A gate that raises false alarms eventually gets switched off, taking the cases it was
right about with it. Precision matters as much as sensitivity in something whose whole
job is to block.

### First production forecast

September 2026, 6 forecast dates, 24 runs, 576 predictions, scored on 144 hours:

| model | MAE MWh | MAPE | bias |
|---|---|---|---|
| EIA DF | 2,857 | 2.624 % | -983 |
| xgb_b | 4,145 | 3.639 % | -3,625 |
| xgb_a | 9,592 | 8.141 % | -4,755 |
| seasonal naive | 13,285 | 11.990 % | -4,808 |

The ordering matches the backtest and the bias is the drift of D-29 showing up live:
xgb_b under-forecasts a rising September by 3,625 MWh. The model was fitted through
August; the diagnosis predicted exactly this, which is the useful thing about having
diagnosed it.

### Schedule

`0 0 11 * * ?` America/New_York — after the 10:00 cutoff — and **PAUSED**. It records
the intended cadence without committing a student credit to unattended runs.

---

## D-35 Asset Bundles — and dev/prod separation that is real, not cosmetic

`databricks.yml` plus `resources/energy_daily_pipeline.job.yml` replace hand-uploading
notebooks, which left no record of what was deployed or from which revision.

### Separating on job name alone would have been a trap

| | dev | prod |
|---|---|---|
| job name | `[dev 20133050] energy_daily_pipeline` | `energy_daily_pipeline` |
| catalog | `energy_dev` | `energy` |
| storage root | `.../_managed_dev` | `.../_managed` |
| **Auto Loader checkpoint** | `.../_checkpoints_dev` | `.../_checkpoints` |

The checkpoint is the one that matters and the one easy to miss. It is
**per-environment state**: sharing it would let a dev run advance prod's offsets, after
which prod skips those files — no error, no alert, just missing data that nothing
attributes to the dev run days earlier.

`catalog` and `checkpoint_root` are therefore bundle variables passed as notebook
parameters, so the two environments differ in *configuration* and never in code. A test
enforces that:

```python
assert '"energy.' not in src      # a hard-coded catalog means dev writes to prod
assert 'dbutils.widgets' in src
```

Verified on a real dev run: 325 s, all seven tasks green, producing 271,989 / 777,600 /
67,375 / 47,832 rows and 0 leakage violations — identical to prod, in a completely
separate catalog. Bronze took 139 s against prod's 61 s precisely *because* the dev
checkpoint was empty and all 4,864 files were genuinely re-ingested. The slower run is
the evidence the isolation holds.

### The /Shared warning was true, and the honest fix was to move

`bundle validate -t prod` warned that `/Workspace/Shared` is writable by every user.
The first attempt declared narrower `permissions` in the bundle — which does not change
the folder's ACL, so prod would have *looked* restricted without being it. The warning
was taken at face value instead and prod moved to a user path.

That is honest for a single owner and wrong for a team: a user path is orphaned the day
that account is deprovisioned. The real answer is a service principal owning prod,
deployed from CI rather than a laptop. Recorded as the gap it is.

### Two syntax errors caught by a test that should have existed sooner

Notebooks are valid Python — `# MAGIC` and `# COMMAND ----------` are comments — so
`ast.parse` is a syntax gate needing no workspace. Without it the feedback loop ran
deploy → job run → task failure, minutes per typo, and a broken f-string cost exactly
that. The test found a second error immediately on being written.

It also carries a design check, which is the part worth keeping: no hard-coded catalog,
parameters declared. Same family as the `.cache()` source test from D-34 — both assert
a *property of the code* rather than behaviour under execution, which is why they catch
what running on the dev substrate never will.

### Sync scope

`data/**` (61 MB of Parquet already on ADLS) and `.venv/**` are excluded. Without that,
every `bundle deploy` would push hundreds of megabytes into the workspace.

### Verified end to end, both targets

The hand-made job was deleted; both jobs are now bundle-managed.

| | dev | prod |
|---|---|---|
| job id | 782233663273637 | 646154696850115 |
| notebook root | `.bundle/energy/dev/files/` | `.bundle/energy/prod/files/` |
| catalog parameter | `energy_dev` | `energy` |
| run | 325 s, 7/7 green | 261 s, 7/7 green |
| **bronze task** | **139 s** | **60 s** |

That 139 s against 60 s is the isolation result, not noise. The dev checkpoint was
empty so all 4,864 files were genuinely re-ingested; prod's checkpoint was untouched by
the dev run, so prod skipped them and its row counts did not move — 271,989 and 777,600
before and after. Had the two shared checkpoint state, prod would have behaved
differently, and the claim in this entry would have been wrong.

Deploying is weaker than running, so prod was run once through the bundle rather than
left validated-but-unexercised.

Reproducibility, incidentally demonstrated: `xgb_b` scored `MAE 4144.7` on the
bundle-deployed prod run, digit for digit what the hand-made job produced. Same seed,
same data, same answer.

---

## D-36 MLflow registry in Unity Catalog — and a speed claim that did not survive measurement

`energy.ml.demand_forecaster`, registered from a separate `energy_monthly_refit` job.
The daily pipeline loads `@champion` instead of fitting.

### Registering is not promoting

Drift was measured in this project — demand rose 11.5% across the backtest and the
model's bias tracked it (D-29) — so a refit is not automatically an improvement. Every
refit registers a version; the alias moves only when the candidate earns it against the
incumbent's **own recorded holdout score**, read from the run that produced it.

```
candidate 3,409.0   champion none      -> PROMOTE  "no champion yet"
candidate 3,409.0   champion 3,409.0   -> PROMOTE  "within 2% of champion"
candidate 2,700.0   champion 2,600.0   -> HOLD     "worse by more than 2%"
```

The 2% band matters in both directions. Without it, month-to-month noise alone flips
the alias and the served model becomes a random walk; too wide and a genuinely better
model never ships.

**The registered model is exactly the model that was scored** — fitted on `train`, not
refitted on everything afterwards. The common alternative serves a marginally better
model whose logged metric describes a *sibling*, and that metric is what the gate
compares against. For something deciding what gets served, the number should describe
the artifact it guards. The cost is two months of recent data unused; at a monthly
cadence the served model is roughly that stale regardless.

### The wrapper carries a contract

An XGBoost Booster has no memory of which columns it was trained on or in what order,
and binds by position once a DMatrix is built. Handed a reordered or short frame it
returns numbers, not an error. The pyfunc wrapper therefore stores the feature list and
reindexes to it, raising on a missing feature. Tested: missing column raises, reordered
columns predict identically, extra columns are ignored.

### Verified against Unity Catalog, not only sqlite

The local tests use a SQLite backend; the job uses `databricks-uc`. Those can differ at
exactly the point that matters, so a second refit was run against UC specifically to
check that `champion_metric` reads the aliased version's metric. It returned
`"within 2% of champion"` rather than `"no champion yet"` — the comparison path works.
Had it not, the gate would have promoted blindly every month, which is precisely what
it exists to prevent.

`holdout_mae` came back as 3,409.044 on both runs, digit for digit. Same seed, same
data, same answer.

### The speed argument was wrong

The stated motivation included avoiding a refit on every daily run. Measured, that is
not where the time goes:

```
refit    126 s   pip install + toPandas + FIT + holdout scoring + log + register
forecast 104 s   pip install + toPandas + load model + predict + write 2 tables
```

The job doing strictly more work is only 22 s slower, so fitting costs roughly 20 s,
not the ~100 s implied. The daily task was never fit-bound: the fixed overhead —
`%pip install`, `restartPython()`, `toPandas()` on 47,832 × 57 — dominates both.

Recorded rather than quietly dropped, because the claim was made before it was
measured. What the registry actually bought is **provenance and control**: every
prediction row now carries `model_version`, a forecast is traceable to the artifact
that produced it, and promotion is a decision with a recorded reason instead of a side
effect of running a job. Those were worth doing on their own.

If the daily runtime does need cutting, the lever is the dependency install rather than
the model: declaring them in the job environment instead of `%pip` removes the restart,
and the baselines need neither xgboost nor mlflow at all.

---

## D-37 Serving the all-data model, and the in-sample flag that took three attempts

D-36 registered exactly the model that was scored — fitted on `train`, holding out two
months. Deployed and measured, that cost **+9.1% MAE**: `xgb_b` scored 4,144.7 when
fitted inline through August and 4,523.9 from the registry fitted only through June.
Under the drift of D-29, the withheld months are the ones that carry the information.

Nine percent is too much to pay for a tidy metric name, so two models are now fitted:

| | trained on | role |
|---|---|---|
| gate | everything before the holdout | scored out-of-sample; decides promotion |
| served | all labelled data | registered, and what actually predicts |

The metric is named **`gate_holdout_mae`** rather than anything that reads as a score of
the served artifact, and `metric_describes` is logged as a param alongside. The original
objection — that a metric should not silently describe a different model — is satisfied
by naming, which costs nothing, instead of by withholding data, which cost 9.1%.

Comparisons stay like-for-like: every version logs the same quantity, computed the same
way. `champion_metric` falls back to the old `holdout_mae` name for versions registered
before the rename, since the quantity is identical and only the label improved.

### The number that should never have been published

The first run after the change reported, in `gold.forecast_accuracy`:

```
xgb_b   MAE 1,573.6   MAPE 1.407%
eia_df  MAE 2,856.7   MAPE 2.624%
```

A 45% win over the balancing authority. Entirely fabricated: the served model is fitted
on all labelled data, and the accuracy table was scoring it on September 2026 — data it
had trained on. Anyone reading that table would have drawn exactly the wrong conclusion,
and this is the failure mode the whole project exists to prevent (D-28).

A caveat in a message is not a fix. The pipeline had to stop publishing it.

### Three attempts, each failing in the flattering direction

`is_in_sample` compares a prediction's target against the cutoff of whatever the model
saw. Getting that cutoff right took three goes:

| | cutoff used | why it was wrong |
|---|---|---|
| 1 | `training_data_end` from a local calendar guess | stale the moment fitting moved to the registry; flagged **0** rows |
| 2 | `max(forecast_date)` read from the artifact's run | a run on day D carries labels for D+1, so it understated the model's knowledge by a day; flagged 93 of 144, leaving 51 in-sample rows scored as honest |
| 3 | `max(target_timestamp_utc)` over labelled training rows | correct; flags all 144 |

The pattern is the lesson. The first two used a **proxy** for "what did the model know",
and a wrong proxy does not raise — it silently widens the gate, always in the direction
that makes the model look better. A guard against leakage has to read the exact
quantity, from the artifact itself, never from a local assumption about it.

Attempt 1 was the worst of the three: it reported `in_sample_excluded_rows: 0`
confidently, which is more dangerous than no flag at all because it looks checked.

### What the table says now

```
out_of_sample       seasonal_naive, eia_df only
in_sample_excluded  144
```

`xgb_b` is absent from the out-of-sample split, and that is the correct answer: with a
backfilled history and a model fitted through it, the challenger has not yet predicted
a single hour it had not already seen. The split fills as time passes.

This also separates two tables that were being conflated:

| | measures | source |
|---|---|---|
| backtest, 55 folds (D-29) | **capability**, genuinely out-of-sample | `scripts/run_backtest.py` |
| `gold.forecast_accuracy` | **operations**, forecasts issued and later scored | the daily job |

Treating the second as a capability measure is what produced the 45% number.

---

## D-38 Measuring the daily task's fixed cost — the optimisation failed, the measurement did not

D-36 ended by naming the dependency install as the lever for cutting the daily runtime.
Instrumented and measured, that was wrong twice over.

### Where the time actually goes

`forecast` task, phase timings reported by the notebook itself:

```
task total        78 s
notebook total    37.2 s
  widgets + sys.path   17.2 s    first spark.sql call — session initialisation
  imports               4.2 s
  toPandas              1.1 s    assumed significant; it is not
  load_champion         7.9 s    artifact download from UC
  predict               0.8 s
  write tables          3.9 s
gap (pre-code setup)  ~41 s      %pip install + restartPython()
```

`toPandas()` on 47,832 × 57 was named as a suspect and costs **1.1 s**. The largest item
inside the notebook is the first `spark.sql` call, which is session startup, not work.

### Moving dependencies to the job environment did not help

```
                        %pip in notebook    job environments
task total                    78 s               93 s
notebook total               37.2 s             35.2 s
setup gap                    ~41 s              ~58 s
```

Task totals across runs were 78, 93 and 104 s while the notebook's own time stayed at
35-37 s, so **all of the variance is in setup**. With one run each, neither declaration
style can be called faster; the honest reading is that they are indistinguishable
against that noise.

**The change is kept anyway, on different grounds than it was proposed for.** The
version pin now lives in the job spec, visible and diffable without opening a notebook,
which is better engineering whether or not it is quicker. Recording that the stated
justification did not survive measurement, because proposing an optimisation and then
quietly keeping it for other reasons is how unmeasured folklore accumulates.

### There was no 85 seconds to remove

That figure came from my own arithmetic on a task total, before any phase was measured.
The real structure is ~35 s of notebook work — most of it Spark session startup — plus
a 40-60 s serverless floor for provisioning a task that needs xgboost and mlflow. Both
are floors, not waste.

If the daily runtime genuinely mattered, the remaining levers are architectural rather
than configurational: the two baselines need neither library and could run as a
separate task without the ml environment, and `load_champion` re-downloads the artifact
every run. Neither is worth doing for a job that runs once a day and costs seconds of
serverless compute.

The instrumentation stays in the notebook. The next person to claim something here is
slow will have numbers to argue with.

---

## D-39 Ingestion becomes a job task — the pipeline stops being fed by hand

Until now the DAG began at Bronze, reading a landing zone a laptop refreshed with
`azcopy`. The schedule would have run daily, reported success, and never seen a new
hour. `00_ingest` is now the first task.

```
ingest → bronze → silver ─┬→ gold
                          └→ features → leakage_audit → forecast → accuracy
```

### Writing to cloud storage from non-Spark code

The extractors use `pathlib` and `pyarrow`, neither of which can address `abfss://`.
A **UC external volume** gives them a real path (`/Volumes/energy/bronze/landing`) over
the same bytes Auto Loader reads through `abfss://`. The alternative — write to driver
disk, copy afterwards — adds a second step that can fail independently of the one that
fetched the data, and then the landing zone holds a partial day with no record of why.

The API key moved from a gitignored `.env` to a Databricks secret scope.
`dbutils.secrets.get` redacts the value in cell output and logs; a widget or an
environment variable would print it.

### Unity Catalog forced a decision that was overdue

Creating the second volume failed:

```
Input path url 'abfss://.../landing' overlaps with other external location
```

UC refuses two external volumes over one path, and it is right to — two names for one
location is ambiguous ownership. That settled a question this project had been
avoiding: with ingestion now **writing** to the landing zone, dev and prod sharing one
would let a dev run put files into prod's source of truth.

`landing_dev` was created by a server-side copy within the same account (4,864 files,
28 MB, 4.5 min), and `landing_root` became a per-target bundle variable alongside
`catalog` and `checkpoint_root`. Dev's Bronze and checkpoint were dropped and rebuilt,
since an Auto Loader checkpoint is bound to its source path.

### The run times are the evidence

| | dev | prod |
|---|---|---|
| ingest | 36 s | 29 s |
| **bronze** | **128 s** | **40 s** |

Same code. Dev's checkpoint had been deleted, so it re-read all 4,864 files; prod's was
intact, so it processed only the 29 new ones. Had prod also taken ~128 s, the checkpoint
would not have been matching its source — a failure that raises nothing and silently
doubles Bronze.

Counts reconcile exactly, and both environments agree through independent paths:

```
eia      271,989 + 1,316 = 273,305      files 2,839 + 15 = 2,854
weather  777,600 + 5,376 = 782,976      files 2,025 + 14 = 2,039
silver    67,375 -> 67,447   (+72 hours, three new days)
features  last forecast_date 2026-09-06 -> 2026-09-09
```

### Revisions: an instrument that now reports

The lookback is 14 days rather than "since the last run", on purpose: upstream revises
recent hours, so re-fetching them is the point. Each pass lands a new vintage and Silver
resolves them; asking only for genuinely new hours would miss every correction.

That lookback produced 644 additional vintage pairs — and `revision_count` reports:

```
hours_with_a_revision       0
max_revisions_on_one_hour   0
```

All 644 were byte-identical re-fetches, which is the distinction D-13 was built to make.

**Stated precisely: no revision was observed in this 14-day window. That is not the same
as EIA not revising.** Published adjustments run on a longer cycle, so a 14-day lookback
may simply be too short to intersect one. What exists now is an instrument and a reading
from it; extending the window costs API calls and vintages, and is worth doing only if
the reading stays at zero long enough to be worth testing.

Until this run the metric was computed and never surfaced — indistinguishable from one
that was broken. It is now in the Silver task's output.

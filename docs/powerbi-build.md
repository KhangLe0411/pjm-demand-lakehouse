# Building the Power BI page — step by step

[`powerbi.md`](powerbi.md) says *what* the page is and why. This says *how* to build it.
Follow it in order; each step names what you should see, so a wrong turn is caught at the
step that caused it rather than three steps later.

Expect 45–60 minutes the first time.

Three things in the spec were wrong until the day this guide was written, and all three
were found by trying to follow it. They are corrected here and in `powerbi.md`:

* the ADLS path it gave does not exist — the Gold tables are UC **managed** tables
* `is_peak` and `is_extreme` were referenced by DAX and were not columns on the table
* "do not point this at a SQL Warehouse" conflated DirectQuery with Import

---

## 0. Before you start

| | |
|---|---|
| Power BI Desktop | Windows only. Free from the Microsoft Store. |
| Account | The same one that owns the workspace — it already has `SELECT` on `energy`. |
| Data | `energy.gold` is populated by the daily job. Nothing to prepare. |

You do **not** need the `energy_dev` catalog for this. Build against `energy`.

---

## 1. Start the SQL warehouse

Power BI reads through a warehouse. It is stopped, and starting it is the only part of
this that costs money.

```
Databricks → SQL Warehouses → lakeobs-wh → Start
```

It is serverless, 2X-Small, and stops itself after 5 minutes idle. Each refresh
therefore costs about one 5-minute window — cents. What would not be cents is a
15-minute scheduled refresh in the Power BI service, which keeps restarting it. Leave
refresh manual.

Wait for **Running** before the next step, or the connection will time out and Power BI
will report it as a credentials error, which it is not.

---

## 2. Connect

`Home → Get Data → More… → Azure → Azure Databricks`

```
Server hostname : adb-7405610310266341.1.azuredatabricks.net
HTTP path       : /sql/1.0/warehouses/fd3d8f60a5743f25
Data Connectivity Mode : Import          ← not DirectQuery
```

Sign in with **Azure Active Directory** (your own account — no token, no password
stored in the file).

Then in the Navigator: `energy` → `gold` → tick

* `forecast_accuracy`
* `daily_summary`
* `forecast_run`

**Load**, not Transform — no shaping is needed.

**Checkpoint.** All three tables appear under Data. `forecast_accuracy` has 20 columns
including `is_peak`, `is_extreme` and `extreme_threshold_mwh`. If those three are
missing, the daily job has not yet run the version that adds them — see §9.

---

## 3. Data model

Power BI will guess relationships. Delete whatever it guessed and create one:

```
forecast_run[forecast_run_id]  1 ──── *  forecast_accuracy[forecast_run_id]
```

Single direction, `forecast_run` filters `forecast_accuracy`.

Leave `daily_summary` **unrelated**. It is a daily grain with its own date column and
joining it to hourly rows on a date creates a many-to-many that silently fans out every
measure. Its visuals stand alone.

Mark no date table. The page filters on `target_local_date` and `local_date` directly,
and a shared date dimension would reintroduce the fan-out you just avoided.

---

## 4. Measures

New measure on `forecast_accuracy`, one at a time. These are the corrected set —
`powerbi.md` has the same list with the reasoning.

```dax
MAE MWh  = AVERAGE ( forecast_accuracy[absolute_error_mwh] )

RMSE MWh = SQRT ( AVERAGEX ( forecast_accuracy, forecast_accuracy[error_mwh] ^ 2 ) )

MAPE %   = 100 * AVERAGE ( forecast_accuracy[ape] )

-- Signed on purpose. A model that is consistently low reads very differently from one
-- that is merely noisy, and ABS() cannot tell them apart.
Bias MWh = AVERAGE ( forecast_accuracy[error_mwh] )

Peak MAPE %    = CALCULATE ( [MAPE %], forecast_accuracy[is_peak] = TRUE () )
Extreme MAPE % = CALCULATE ( [MAPE %], forecast_accuracy[is_extreme] = TRUE () )

-- Pinned regardless of the model slicer, so a card can show the gap while the rest of
-- the page is filtered to one challenger.
Benchmark MAE =
    CALCULATE ( [MAE MWh],
        ALL ( forecast_accuracy[model] ),
        forecast_accuracy[model] = "eia_df" )

Gap vs benchmark % = 100 * DIVIDE ( [MAE MWh] - [Benchmark MAE], [Benchmark MAE] )
```

One more, and it is the important one:

```dax
-- Every measure above must be read on out-of-sample rows only. A model scored on hours
-- it was fitted on looks far better than it is: measured here at MAE 1,573 against the
-- benchmark's 2,856, which would read as beating a professional forecast by 45%.
Rows scored = CALCULATE ( COUNTROWS ( forecast_accuracy ),
                          forecast_accuracy[is_in_sample] = FALSE () )
```

**Checkpoint.** Put `MAE MWh` on a card with `model` on rows. `eia_df` should be near
2,880 and `seasonal_naive` near 13,585. If `xgb_b` looks dramatically better than
`eia_df`, you are reading in-sample rows — fix it in §5 before going further.

---

## 5. The filter that must be on the page

Add a **page-level filter**:

```
forecast_accuracy[is_in_sample] is False
```

Page level, not visual level. A visual-level filter protects one visual and leaves the
next one you add unprotected, and the failure is silent — the number just looks good.

The backfilled history is why this exists: the served model is fitted on all labelled
data, so part of the scored range was seen during fitting. In live operation the overlap
does not arise. On this page it would, every time, without the filter.

---

## 6. The page

Four rows, top to bottom.

**KPI cards** — `MAE MWh`, `Bias MWh`, `MAPE %`, `Gap vs benchmark %`

Put `Bias MWh` immediately next to `MAE MWh`. Not for layout: during Winter Storm
Elliott all 72 event hours were under-forecast and MAE equalled |bias| exactly. A MAE
card alone shows a bigger number and not the fact that every bit of it is in one
direction.

**Main chart** — line, X = `target_timestamp_utc`, Y = `actual_mwh` and `forecast_mwh`,
legend = `model`.

**Peak section** — table, rows = `target_local_date`, values = `Peak MAPE %` and
`peak_timing_error_hours` from `daily_summary`.

**Benchmark section** — bar, axis = `model`, value = `MAE MWh`, with a constant line at
`Benchmark MAE`.

**Slicers** — `model`, `target_local_date` (range), `is_extreme`.

---

## 7. The two things the page must not do

Both are in `powerbi.md`; repeated because they are easy to drop while arranging
visuals.

**Never show an average that hides a one-sided error.** `Bias MWh` beside `MAE MWh`,
everywhere accuracy appears.

**Never show peak-timing accuracy without the regime split.** The 48.4% headline hit
rate is an average over days whose load shape never inverts. On 24–25 December 2022 the
peak moved to 08:00–09:00 and every model, benchmark included, missed it by 9–13 hours.
That is what the `is_extreme` slicer is for — see [`case-study-elliott.md`](case-study-elliott.md).

A useful sanity reading: in the current window roughly a quarter of scored hours are
flagged extreme, against the 5% the threshold nominally selects. The threshold is the
P95 of *all history through the training cutoff* (README section 19), so a seasonally
hot stretch legitimately trips it far more often. That is the split doing its job, not a
miscount — but it means "extreme" here reads as *high for the year*, not *rare*.

---

## 8. Refresh, and stopping the meter

```
Home → Refresh          (starts the warehouse, pulls, finishes)
Databricks → SQL Warehouses → lakeobs-wh → Stop
```

The warehouse stops itself after 5 minutes; stopping it by hand just skips the wait.

Do not publish with a scheduled refresh unless you want the warehouse woken on that
schedule forever. Manual refresh before a demo is the whole requirement here.

---

## 9. When it goes wrong

| Symptom | Cause |
|---|---|
| Connection times out | The warehouse was still starting. Wait for **Running**, retry. |
| `is_peak` / `is_extreme` missing | The prod job has not run the version that adds them. They arrive with the next daily run after that change is deployed. |
| `xgb_b` beats `eia_df` by a wide margin | The in-sample filter (§5) is missing or is visual-level. |
| Every measure is inflated or doubled | `daily_summary` got related to `forecast_accuracy`. Delete the relationship (§3). |
| MAPE is enormous on a few rows | Hours where actual demand is near zero. Filter them or use MAE; `ape` divides by the actual. |
| Refresh works, published report fails | The service needs a gateway or its own credentials. Out of scope — this page is built to be demonstrated from Desktop. |

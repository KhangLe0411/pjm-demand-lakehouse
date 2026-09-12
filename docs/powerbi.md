# Power BI — one page (README section 26)

A `.pbix` is a proprietary binary and is not generated here. Everything around it is:
the tables in the shape the report consumes, the model, the DAX, and a layout preview
rendered from real numbers at
[`docs/diagrams/powerbi_preview.html`](diagrams/powerbi_preview.html).

## Connection — Azure Databricks connector, Import mode

```
Get Data → Azure Databricks
   Server hostname : adb-7405610310266341.1.azuredatabricks.net
   HTTP path       : /sql/1.0/warehouses/fd3d8f60a5743f25
   Catalog/schema  : energy / gold
Mode: Import
```

**This section previously said to read ADLS Gen2 directly at
`abfss://…/gold/forecast_accuracy`. That path does not exist.** Every Gold table is a
Unity Catalog *managed* table, so its files live under
`_managed/__unitystorage/catalogs/<guid>/tables/<guid>` — opaque identifiers that change
if a table is recreated, and reading them directly bypasses UC entirely. The
warehouse-free plan was written against a layout this project does not have.

**Import, not DirectQuery — the distinction the original text lost.** DirectQuery or a
scheduled refresh does restart the warehouse on every interaction, and that is the
expensive pattern D-12 warned about. Import runs the query once per refresh and then
serves from memory: the warehouse is serverless, 2X-Small, auto-stopping after 5
minutes, so a manual refresh costs roughly one 5-minute idle window. Refreshing when you
demo costs cents; a 15-minute scheduled refresh does not, and is the thing to avoid.

At ~158k rows the whole model is a few MB in memory.

## Data model

```
forecast_run  1 ──< forecast_prediction
      │ 1
      └──< forecast_accuracy >── 1  dim_date (local_date)
```

| Table | Grain | Rows | Role |
|---|---|---|---|
| `forecast_run` | one forecasting event | 6,604 | what the model knew: cutoff, training end, version |
| `forecast_prediction` | one target hour | 158,492 | what it said |
| `forecast_accuracy` | one scored hour | 158,061 | how it did, once actuals arrived |
| `daily_summary` | one local operating day | 2,809 | peaks, load factor, completeness |

`model` is the slicer column throughout — it is what makes one page serve the
benchmark comparison instead of needing four.

Mark `dim_date` as a date table on `local_date`. Without it, time intelligence silently
falls back to Power BI's auto date hierarchy, which does not know that some operating
days are 23 or 25 hours long.

## Measures

```dax
MAE MWh      = AVERAGE ( forecast_accuracy[absolute_error_mwh] )
RMSE MWh     = SQRT ( AVERAGEX ( forecast_accuracy,
                                 forecast_accuracy[error_mwh] ^ 2 ) )
MAPE %       = 100 * AVERAGE ( forecast_accuracy[ape] )

-- Signed on purpose. A model that is consistently low reads very differently from one
-- that is merely noisy, and ABS() cannot tell them apart.
Bias MWh     = AVERAGE ( forecast_accuracy[error_mwh] )

Peak MAPE %  = CALCULATE ( [MAPE %], forecast_accuracy[is_peak] = TRUE () )
Extreme MAPE % = CALCULATE ( [MAPE %], forecast_accuracy[is_extreme] = TRUE () )

-- Benchmark pinned regardless of the model slicer, so a card can show the gap while
-- the rest of the page is filtered to one challenger.
Benchmark MAE =
    CALCULATE ( [MAE MWh],
        ALL ( forecast_accuracy[model] ),
        forecast_accuracy[model] = "eia_df" )

Gap vs benchmark % = 100 * DIVIDE ( [MAE MWh] - [Benchmark MAE], [Benchmark MAE] )

Peak timing hit % =
    VAR days = DISTINCTCOUNT ( forecast_accuracy[target_local_date] )
    VAR hits =
        COUNTROWS ( FILTER ( VALUES ( forecast_accuracy[target_local_date] ),
            CALCULATE ( MAX ( forecast_accuracy[hour_of_day] ),
                        forecast_accuracy[is_peak] = TRUE () )
            = CALCULATE ( MAXX ( TOPN ( 1, forecast_accuracy,
                                        forecast_accuracy[forecast_mwh] ),
                                 forecast_accuracy[hour_of_day] ) ) ) )
    RETURN 100 * DIVIDE ( hits, days )
```

## Page layout

**KPI row** — four cards
`Benchmark MAPE` · `Challenger MAPE` · weather contribution (−50.4 %) ·
leakage violations (0)

**Main chart** — line, hourly, last 7 operating days
`actual_mwh` · `forecast_mwh` for `eia_df` · `forecast_mwh` for `xgb_b`

**Benchmark table** — model × {MAE, MAPE, peak MAPE, gap vs benchmark}

**Peak section** — predicted vs actual peak, magnitude error, timing error

**Slicers** — `model`, `local_date` range, `is_extreme`

## Two things the page must not do

**Do not report an average that hides a one-sided error.** During Winter Storm Elliott
every one of 72 event hours was under-forecast — MAE equalled |bias| exactly. A MAE
card alone shows a larger number and not the fact that it is all in one direction. Put
`Bias MWh` next to `MAE MWh` wherever accuracy is shown.

**Do not show peak-timing hit rate without a regime split.** The 48.4 % headline is an
average over days whose load shape never inverts. On 24-25 December 2022 the peak moved
to 08:00-09:00 and every model, benchmark included, missed it by 9-13 hours. See
[`case-study-elliott.md`](case-study-elliott.md).

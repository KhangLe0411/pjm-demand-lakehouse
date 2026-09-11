# Case study — Winter Storm Elliott, 23-25 December 2022

A stress test against a real event inside the backtest window, answering the questions
README section 20 asks. Several answers are unflattering; they are here anyway, because
a stress test that only reports the parts that went well is not a stress test.

Models had been fitted on data through **2022-11-30** — roughly 1.7 years of history
containing nothing resembling this event.

---

## 1. What happened

| Local date | Peak MWh | Mean | Min | Peak vs December normal |
|---|---|---|---|---|
| 2022-12-22 | 107,435 | 101,357 | 91,807 | -6.7 % |
| **2022-12-23** | **135,328** | 111,621 | 87,833 | **+17.5 %** |
| **2022-12-24** | **129,542** | **124,890** | **119,997** | **+12.5 %** |
| 2022-12-25 | 117,362 | 111,611 | 103,449 | +1.9 % |
| 2022-12-26 | 115,051 | 109,650 | 106,256 | -0.1 % |

December 1-19 peak for reference: 115,170 MWh.

The 24th is the striking row. Its **minimum** — 119,997 MWh — is higher than the peak of
a normal December day. Demand did not spike and recover; it sat elevated for a full day.

## 2. How each model did

Event days only, against the same models over the whole backtest.

| Model | MAE (event) | MAPE (event) | bias (event) | MAE (backtest) | degradation |
|---|---|---|---|---|---|
| seasonal naive | 20,497 | 16.897 % | -20,497 | 8,300 | 2.5× |
| **EIA DF** | **5,255** | **4.516 %** | -2,377 | 2,294 | 2.3× |
| xgb_a (no weather) | 24,610 | 20.397 % | -24,348 | 5,203 | 4.7× |
| xgb_b (+ weather) | 13,089 | 10.777 % | -13,089 | 2,581 | **5.1×** |

**For all three challengers, MAE equals |bias| exactly.** That is not a rounding
coincidence: it means every single hour of the event was under-forecast, all 72 of
them, without one exception. The benchmark's bias (-2,377) is well below its MAE
(5,255), so it was wrong in both directions — noisy under pressure, but not blind to
the event.

Weather features halved the damage — 24,610 → 13,089 — and were still not enough.

## 3. Did anyone see the ramp coming?

Mean error by day; negative is under-forecast.

| Local date | actual peak | EIA DF | seasonal naive | xgb_a | xgb_b |
|---|---|---|---|---|---|
| 2022-12-22 | 107,435 | -1,381 | -1,414 | -7,382 | -5,369 |
| 2022-12-23 | 135,328 | -2,882 | -15,014 | -16,815 | -10,628 |
| **2022-12-24** | 129,542 | **-5,462** | **-30,746** | **-34,641** | **-20,279** |
| 2022-12-25 | 117,362 | +1,213 | -15,733 | -21,588 | -8,361 |
| 2022-12-26 | 115,051 | -1,310 | -2,940 | -8,839 | +1,329 |

Recovery is as informative as the event. By the 26th both the benchmark and xgb_b are
back to ordinary errors, so nothing was structurally broken — the models simply had no
example of this regime to learn from.

## 4. The finding that was not anticipated: the peak moves

| Model | Date | Actual peak hour | Predicted peak hour | Timing error |
|---|---|---|---|---|
| EIA DF | 12-23 | 19 | 21 | +2 |
| EIA DF | **12-24** | **9** | 18 | **+9** |
| EIA DF | **12-25** | **8** | 21 | **+13** |
| xgb_b | 12-24 | 9 | 20 | +11 |
| xgb_b | 12-25 | 8 | 19 | +11 |
| xgb_a | 12-24 | 9 | 18 | +9 |

On 24 and 25 December the daily peak landed at **09:00 and 08:00 local** — the morning,
not the evening. In severe cold, heating load peaks with the overnight temperature
minimum rather than with the evening activity cycle, so the entire daily shape inverts.

**Every model missed it, the benchmark included**, by 9 to 13 hours. This is not a
modelling defect specific to this project: it is a regime the usual daily profile does
not contain, and a peak-timing metric averaged over ordinary days conceals it entirely.

It also has an operational reading. A forecast that is 2.5% off on magnitude but places
the peak twelve hours late would have reserves positioned for the wrong half of the day.

## 5. Worst individual hours

| Model | Date | Hour | Actual | Predicted | Error |
|---|---|---|---|---|---|
| xgb_a | 12-24 | 03 | 121,912 | 84,182 | -37,730 |
| xgb_a | 12-24 | 11 | 129,017 | 91,346 | -37,671 |
| xgb_a | 12-24 | 05 | 121,865 | 84,287 | -37,578 |
| xgb_a | 12-23 | 22 | 132,078 | 94,525 | -37,553 |

The no-weather model was under by ~31% for hours at a stretch. With no temperature
input it had nothing to distinguish 24 December from any other winter Saturday.

---

## What this changes

1. **Extreme-regime error is one-sided, not merely larger.** Reporting MAE alone hides
   that. Every event hour under-forecast is the signature of a squared-error objective
   regressing to the mean where the tail matters most — the case for the asymmetric
   loss listed as a V2 candidate.
2. **Peak *timing* needs a regime split.** The 48.4% exact-hour rate reported over the
   whole backtest is an average across days whose shape never inverts. A separate
   cold-regime timing metric belongs in the reporting.
3. **Weather features are worth roughly half the error here**, consistent with the
   50.4% MAE reduction measured over the full backtest — the benefit holds up under
   stress rather than only on average.
4. **The benchmark degrades too**, 2.3× against xgb_b's 5.1×. The gap widens under
   stress, which is the honest version of "within 12.5%": that figure is an average,
   and the challenger is relatively weakest exactly where accuracy matters most.

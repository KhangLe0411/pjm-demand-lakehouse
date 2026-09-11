"""Declared feature provenance, and the audit that checks it.

README section 31 lists "feature generation passes a leakage audit" in the definition of
done. A review step cannot discharge that: leakage is invisible by construction, so the
audit has to be mechanical. Every feature therefore declares where its information comes
from, and `audit()` computes when that information actually became knowable and compares
it with the cutoff.

Two things make this more than bookkeeping.

**Publication lag.** An observation existing is not the same as it being available. The
EIA feed was measured on 2026-09-08 (docs/decisions.md D-24):

    D   1.41 h      DF  3.41 h      NG  28.41 h      TI  28.41 h

So at a 10:00 cutoff the 09:00 demand does not exist yet, and `demand_lag_1h` — which
looked obviously fine — is a leak. Lag is modelled per metric with margin rather than
assumed to be zero.

**Weather run time.** A forecast's provenance is its *run* instant, not its valid
instant. Open-Meteo exposes only "N days earlier", so the run is bounded by the end of
that local date. For lead 2 that is provably before a next-morning cutoff; for lead 1 it
is not, and the audit says so rather than waving it through.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from src.silver.calendar_utils import PJM_TZ, UTC, local_day_bounds


def _as_utc(dt: datetime) -> datetime:
    """Normalise to tz-aware UTC.

    Spark returns naive timestamps — under a UTC session, which both `local_session()`
    and the job's `spark_conf` pin — while the datetimes constructed here are aware.
    Comparing the two raises, so they are reconciled once at the boundary instead of
    every caller having to remember. A naive value is read as UTC, which is true given
    that session setting and false without it; that is why the setting is pinned in
    both places rather than left to the default.
    """
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class Anchor(str, Enum):
    CUTOFF = "cutoff"      # offset measured back from the forecast cutoff
    TARGET = "target"      # offset measured back from the target hour
    CALENDAR = "calendar"  # deterministic; knowable arbitrarily far ahead
    WEATHER = "weather"    # provenance is the forecast run, via lead_days


# Measured, then rounded up for margin. A single observation is not a distribution, so
# the margin is doing real work; re-measure with scripts/measure_publication_lag.py.
PUBLICATION_LAG_HOURS: dict[str, float] = {"D": 3.0, "DF": 5.0, "NG": 30.0, "TI": 30.0}


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    anchor: Anchor
    offset_hours: float = 0.0        # hours BACK from the anchor
    metric: str | None = None        # D / DF / NG / TI -> applies publication lag
    lead_days: int | None = None     # weather vintage
    note: str = ""

    def available_at(self, cutoff_utc: datetime, target_utc: datetime,
                     tz: str = PJM_TZ) -> datetime | None:
        """Instant this feature's information first becomes knowable.

        None means always knowable, which only calendar features may claim.
        """
        cutoff_utc, target_utc = _as_utc(cutoff_utc), _as_utc(target_utc)
        if self.anchor is Anchor.CALENDAR:
            return None
        if self.anchor is Anchor.WEATHER:
            # Run date is `lead_days` local days before the target's local date; the run
            # is bounded by the end of that date, i.e. the start of the next one.
            target_local_date = target_utc.astimezone(
                __import__("zoneinfo").ZoneInfo(tz)).date()
            run_date = target_local_date - timedelta(days=self.lead_days or 0)
            _, end_of_run_date = local_day_bounds(run_date, tz)
            return end_of_run_date
        base = cutoff_utc if self.anchor is Anchor.CUTOFF else target_utc
        observed = base - timedelta(hours=self.offset_hours)
        lag = PUBLICATION_LAG_HOURS.get(self.metric or "", 0.0)
        return observed + timedelta(hours=lag)


@dataclass(frozen=True)
class Violation:
    feature: str
    available_at: datetime
    cutoff_utc: datetime
    target_utc: datetime

    @property
    def hours_late(self) -> float:
        return (self.available_at - self.cutoff_utc).total_seconds() / 3600

    def __str__(self) -> str:
        return (f"{self.feature}: knowable at {self.available_at.isoformat()}, "
                f"{self.hours_late:+.2f}h relative to cutoff "
                f"{self.cutoff_utc.isoformat()} (target {self.target_utc.isoformat()})")


def audit(specs: list[FeatureSpec], cutoff_utc: datetime, target_utc: datetime,
          tz: str = PJM_TZ) -> list[Violation]:
    """Every violation for one (cutoff, target) pair, worst first."""
    cutoff_utc, target_utc = _as_utc(cutoff_utc), _as_utc(target_utc)
    out = []
    for s in specs:
        at = s.available_at(cutoff_utc, target_utc, tz)
        if at is not None and at > cutoff_utc:
            out.append(Violation(s.name, at, cutoff_utc, target_utc))
    return sorted(out, key=lambda v: -v.hours_late)


# --------------------------------------------------------------- feature registry

WEATHER_POINTS = ("chicago_comed", "philadelphia_peco", "washington_pepco",
                  "baltimore_bge", "newark_pseg", "pittsburgh_duq",
                  "cleveland_atsi", "richmond_dom")

PRIMARY_LEAD = 2   # the provably-available vintage; see D-21


def _demand_features() -> list[FeatureSpec]:
    out = [
        # Recent level, anchored to the cutoff. Offsets start at 3h, not 1h, because
        # of the measured D publication lag.
        *[FeatureSpec(f"demand_cutoff_minus_{h}h", Anchor.CUTOFF, h, "D")
          for h in (3, 4, 5, 6, 27)],
        FeatureSpec("demand_rolling_mean_24h_to_cutoff", Anchor.CUTOFF, 3, "D",
                    note="window ends at cutoff-3h, so its newest input is cutoff-3h"),
        FeatureSpec("demand_rolling_std_24h_to_cutoff", Anchor.CUTOFF, 3, "D"),
        # Same clock hour history, anchored to the target. 48h is the smallest safe
        # offset: the maximum horizon is 38h, so t-24h would be unavailable for every
        # target hour past 21h ahead (docs/decisions.md D-04).
        *[FeatureSpec(f"demand_same_hour_minus_{d}d", Anchor.TARGET, 24 * d, "D")
          for d in (2, 7, 14)],
        FeatureSpec("demand_same_hour_prev_week_mean", Anchor.TARGET, 48, "D",
                    note="mean over t-168h..t-48h at the target clock hour"),
    ]
    return out


def _weather_features(lead: int = PRIMARY_LEAD) -> list[FeatureSpec]:
    out = [FeatureSpec(f"temp_c_{p}_lead{lead}", Anchor.WEATHER, lead_days=lead)
           for p in WEATHER_POINTS]
    out += [
        FeatureSpec(f"temp_c_region_mean_lead{lead}", Anchor.WEATHER, lead_days=lead),
        FeatureSpec(f"cdd_region_lead{lead}", Anchor.WEATHER, lead_days=lead),
        FeatureSpec(f"hdd_region_lead{lead}", Anchor.WEATHER, lead_days=lead),
        FeatureSpec(f"temp_c_target_day_max_lead{lead}", Anchor.WEATHER, lead_days=lead),
        FeatureSpec(f"temp_c_target_day_min_lead{lead}", Anchor.WEATHER, lead_days=lead),
        FeatureSpec(f"cdd_target_day_sum_lead{lead}", Anchor.WEATHER, lead_days=lead),
        FeatureSpec(f"hdd_target_day_sum_lead{lead}", Anchor.WEATHER, lead_days=lead),
    ]
    return out


def _calendar_features() -> list[FeatureSpec]:
    return [FeatureSpec(n, Anchor.CALENDAR) for n in (
        "target_local_hour", "day_of_week", "month", "day_of_year",
        "is_weekend", "is_holiday", "days_to_nearest_holiday")]


# Model A: no weather. Model B: plus weather. Both share demand + calendar, so the
# ablation isolates exactly the weather contribution (docs/decisions.md D-02).
FEATURES_MODEL_A: list[FeatureSpec] = _demand_features() + _calendar_features()
FEATURES_MODEL_B: list[FeatureSpec] = FEATURES_MODEL_A + _weather_features()

# Carried for the sensitivity analysis, NOT for the primary model: lead 1 is expected to
# fail the audit because its run instant cannot be proven to precede the cutoff.
FEATURES_LEAD1_VARIANT: list[FeatureSpec] = _weather_features(lead=1)

# Excluded on purpose, kept so the audit is demonstrated to have teeth rather than
# merely to pass. Each of these looks reasonable and leaks.
KNOWN_LEAKY: list[FeatureSpec] = [
    FeatureSpec("demand_cutoff_minus_1h", Anchor.CUTOFF, 1, "D",
                note="09:00 demand does not exist at a 10:00 cutoff: D lag is ~1.4h"),
    FeatureSpec("demand_same_hour_minus_1d", Anchor.TARGET, 24, "D",
                note="unavailable once the horizon passes ~21h"),
    FeatureSpec("actual_demand_at_target", Anchor.TARGET, 0, "D",
                note="the label itself"),
    FeatureSpec("net_generation_same_hour_minus_2d", Anchor.TARGET, 48, "NG",
                note="NG publication lag is ~28h, so even t-48h is not available"),
    FeatureSpec("temp_c_region_mean_lead0", Anchor.WEATHER, lead_days=0,
                note="observed weather at the target hour - hindsight"),
]

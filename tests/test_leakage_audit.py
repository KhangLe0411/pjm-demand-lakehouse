"""The leakage audit, tested from both directions.

A leakage check that only ever passes is indistinguishable from no check at all, so
half of these assert that deliberately leaky features are *caught*.
"""
from datetime import date, timedelta

import pytest

from src.features.contract import cutoff_utc, horizon_hours, target_hours_utc
from src.features.spec import (
    FEATURES_LEAD1_VARIANT, FEATURES_MODEL_A, FEATURES_MODEL_B, KNOWN_LEAKY,
    Anchor, FeatureSpec, audit,
)

# Ordinary days plus both DST transitions in both directions.
SAMPLE_DATES = [
    date(2024, 1, 15), date(2024, 6, 20), date(2024, 9, 3),
    date(2026, 3, 7), date(2026, 3, 8),      # around spring forward
    date(2026, 10, 31), date(2026, 11, 1),   # around fall back
]


def every_pair(dates=SAMPLE_DATES):
    for d in dates:
        c = cutoff_utc(d)
        for t in target_hours_utc(d):
            yield d, c, t


# --------------------------------------------------------- the contract itself

def test_horizon_stays_within_the_declared_range():
    hs = [horizon_hours(c, t) for _, c, t in every_pair()]
    assert min(hs) >= 13.0 and max(hs) <= 39.0


def test_target_day_length_follows_the_calendar():
    assert len(target_hours_utc(date(2026, 3, 7))) == 23    # D+1 springs forward
    assert len(target_hours_utc(date(2026, 10, 31))) == 25   # D+1 falls back
    assert len(target_hours_utc(date(2024, 6, 20))) == 24


def test_cutoff_utc_instant_shifts_with_dst():
    """10:00 local is 14:00 UTC in EDT and 15:00 UTC in EST."""
    assert cutoff_utc(date(2026, 6, 1)).hour == 14
    assert cutoff_utc(date(2026, 1, 5)).hour == 15


# ------------------------------------------------------------- the models pass

def test_model_a_never_leaks():
    for d, c, t in every_pair():
        v = audit(FEATURES_MODEL_A, c, t)
        assert not v, f"{d}: {[str(x) for x in v]}"


def test_model_b_never_leaks():
    for d, c, t in every_pair():
        v = audit(FEATURES_MODEL_B, c, t)
        assert not v, f"{d}: {[str(x) for x in v]}"


def test_model_b_holds_across_two_full_years():
    """Cheap enough to check exhaustively rather than by sampling."""
    d = date(2024, 1, 1)
    while d < date(2026, 1, 1):
        c = cutoff_utc(d)
        for t in target_hours_utc(d):
            assert not audit(FEATURES_MODEL_B, c, t), f"{d} {t}"
        d += timedelta(days=1)


# --------------------------------------------------- the audit has teeth

@pytest.mark.parametrize("spec", KNOWN_LEAKY, ids=lambda s: s.name)
def test_each_known_leaky_feature_is_caught(spec):
    """Every one of these looks defensible and leaks. If the audit stops catching one,
    the audit is broken, not the feature."""
    caught = any(audit([spec], c, t) for _, c, t in every_pair())
    assert caught, f"audit failed to catch {spec.name}: {spec.note}"


def test_the_label_itself_is_caught_at_every_target_hour():
    label = FeatureSpec("actual_demand_at_target", Anchor.TARGET, 0, "D")
    for _, c, t in every_pair():
        assert audit([label], c, t)


def test_lag_24h_leaks_only_past_a_certain_horizon():
    """Documents precisely why 48h is the smallest safe target-anchored offset."""
    spec = FeatureSpec("demand_same_hour_minus_1d", Anchor.TARGET, 24, "D")
    safe, leaky = [], []
    for _, c, t in every_pair():
        (leaky if audit([spec], c, t) else safe).append(horizon_hours(c, t))
    assert safe and leaky                       # both regimes occur
    assert max(safe) <= 21.0 < min(leaky)       # the boundary is horizon ~21h


def test_publication_lag_is_what_makes_short_offsets_leak():
    """With no lag, cutoff-1h would be fine. The lag is the whole reason it is not."""
    d = date(2024, 6, 20)
    c, t = cutoff_utc(d), target_hours_utc(d)[12]
    assert audit([FeatureSpec("d_1h", Anchor.CUTOFF, 1, "D")], c, t)
    assert not audit([FeatureSpec("no_metric_1h", Anchor.CUTOFF, 1, None)], c, t)


# ------------------------------------------------- the lead-1 weather question

def test_lead1_weather_fails_the_strict_audit():
    """The honest outcome, not a bug. Open-Meteo does not publish the archived run hour,
    so a run from day D cannot be proven to precede a 10:00 cutoff on day D. Lead 1 is
    therefore a sensitivity variant and lead 2 is the primary (docs/decisions.md D-21).
    """
    d = date(2024, 6, 20)
    c, t = cutoff_utc(d), target_hours_utc(d)[0]
    v = audit(FEATURES_LEAD1_VARIANT, c, t)
    assert v, "lead 1 unexpectedly passed - check the run-instant bound"
    assert all(0 < x.hours_late <= 24 for x in v)


def test_lead2_weather_passes_where_lead1_does_not():
    d = date(2024, 6, 20)
    c = cutoff_utc(d)
    for t in target_hours_utc(d):
        assert not audit([s for s in FEATURES_MODEL_B
                          if s.anchor is Anchor.WEATHER], c, t)


def test_observed_weather_at_target_is_caught():
    spec = FeatureSpec("temp_observed_at_target", Anchor.WEATHER, lead_days=0)
    d = date(2024, 6, 20)
    assert audit([spec], cutoff_utc(d), target_hours_utc(d)[18])


# ---------------------------------------------------------------- calendar only

def test_calendar_features_are_the_only_ones_exempt():
    cal = [s for s in FEATURES_MODEL_A if s.anchor is Anchor.CALENDAR]
    assert cal
    d = date(2024, 6, 20)
    for s in cal:
        assert s.available_at(cutoff_utc(d), target_hours_utc(d)[0]) is None
    for s in FEATURES_MODEL_B:
        if s.anchor is not Anchor.CALENDAR:
            assert s.available_at(cutoff_utc(d), target_hours_utc(d)[0]) is not None


def test_audit_accepts_naive_datetimes_from_spark():
    """Spark `collect()` returns naive timestamps under a UTC session; the datetimes
    built here are aware. Mixing them raised `can't compare offset-naive and
    offset-aware datetimes` and failed the deployed leakage-audit task while every
    local test passed — because the local caller happened to pass aware values.
    """
    d = date(2024, 6, 20)
    c, t = cutoff_utc(d), target_hours_utc(d)[18]
    naive_c, naive_t = c.replace(tzinfo=None), t.replace(tzinfo=None)

    assert audit(FEATURES_MODEL_B, naive_c, naive_t) == []          # must not raise
    assert audit(FEATURES_MODEL_B, c, t) == []
    # and the verdict must not depend on which form was passed
    assert len(audit(FEATURES_LEAD1_VARIANT, naive_c, naive_t)) == \
           len(audit(FEATURES_LEAD1_VARIANT, c, t))

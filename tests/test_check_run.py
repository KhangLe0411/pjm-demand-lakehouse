"""The post-run checker must fail on the cases it exists for.

`scripts/check_run.py` guards against a run that is green but empty. That guard is only
worth having if its failure path works, and the failure path is the one never exercised
in normal operation — every real run passes. These feed it the shapes a broken run
actually produces.
"""
import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_run", pathlib.Path("scripts/check_run.py"))
check_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_run)


def _healthy():
    return {
        "ingest": {"eia": {"rows": 1316}, "weather": {"rows": 5376}},
        "bronze": {"tables": {"eia_region_data": 273305, "weather_forecast": 782976},
                   "distinct_source_files": {"eia_region_data": 2854,
                                             "weather_forecast": 2039}},
        "silver": {"electricity_hourly": 67447, "weather_forecast_hourly": 766848,
                   "electricity_quarantine": 7},
        "features": {"rows": 4200, "labelled": 4100},
        "leakage_audit": {"pairs": 384, "violations": 0, "known_leaky_caught": 5},
    }


def test_a_healthy_run_passes():
    assert check_run.evaluate(_healthy()) == []


@pytest.mark.parametrize("task,mutation,expected_substring", [
    # The case this script exists for: Auto Loader saw no new files, everything
    # downstream ran on nothing, and every task still terminated SUCCESS.
    ("ingest", lambda d: d["eia"].__setitem__("rows", 0), "eia rows fetched"),
    ("bronze", lambda d: d["tables"].__setitem__("weather_forecast", 0),
     "every bronze table non-empty"),
    ("silver", lambda d: d.__setitem__("electricity_hourly", 0),
     "electricity_hourly non-empty"),
    ("features", lambda d: d.__setitem__("labelled", 0), "some rows are labelled"),
    ("leakage_audit", lambda d: d.__setitem__("violations", 3), "no leakage violations"),
    # Zero violations over zero pairs is a vacuous pass, which reads identically to a
    # real one in a log.
    ("leakage_audit", lambda d: d.__setitem__("pairs", 0), "audit examined pairs"),
])
def test_a_broken_run_is_caught(task, mutation, expected_substring):
    outputs = _healthy()
    mutation(outputs[task])
    failures = check_run.evaluate(outputs)
    assert any(expected_substring in f for f in failures), failures


def test_a_missing_task_is_reported_not_skipped():
    outputs = _healthy()
    del outputs["leakage_audit"]
    failures = check_run.evaluate(outputs)
    assert any("leakage_audit" in f and "no JSON exit value" in f for f in failures)


def test_a_malformed_exit_value_does_not_pass_silently():
    outputs = _healthy()
    outputs["silver"] = {"unexpected": "shape"}
    failures = check_run.evaluate(outputs)
    assert failures and all("silver" in f for f in failures if "silver" in f)

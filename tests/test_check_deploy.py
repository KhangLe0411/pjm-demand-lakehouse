"""The deploy drift checker must fail on the drifts it exists for.

Each case below is a state `bundle deploy` reports as success. They are the reason the
script exists, and none of them occur during a healthy deploy — so without these the
failure path would ship untested.
"""
import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_deploy", pathlib.Path("scripts/check_deploy.py"))
check_deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_deploy)

ROOT = "/Workspace/Users/sp-id/.bundle/energy/prod"
SP = {"service_principal_name": "sp-id"}
SCHEDULE = {"pause_status": "PAUSED", "quartz_cron_expression": "0 0 11 * * ?",
            "timezone_id": "America/New_York"}


def _job(run_as=None, schedule=None, tasks=None):
    return {
        "run_as": run_as if run_as is not None else dict(SP),
        "schedule": schedule if schedule is not None else dict(SCHEDULE),
        "tasks": tasks if tasks is not None else [
            {"task_key": "ingest",
             "notebook_task": {"notebook_path": f"{ROOT}/files/notebooks/00_ingest"}},
            {"task_key": "silver",
             "notebook_task": {"notebook_path": f"{ROOT}/files/notebooks/02_silver"}},
        ],
    }


def _state():
    return ({"energy_daily_pipeline": _job()},
            {"energy_daily_pipeline": "646154696850115"},
            {"energy_daily_pipeline": _job()})


def test_a_matching_deployment_passes():
    declared, ids, live = _state()
    assert check_deploy.evaluate(ROOT, declared, ids, live) == []


def test_a_declared_job_that_was_never_deployed_is_caught():
    declared, ids, live = _state()
    ids["energy_daily_pipeline"] = None
    failures = check_deploy.evaluate(ROOT, declared, ids, live)
    assert any("declared but not deployed" in f for f in failures), failures


def test_run_as_reverting_to_a_human_is_caught():
    """The D-40 invariant. A job deployed without run_as runs as whoever deployed it."""
    declared, ids, live = _state()
    live["energy_daily_pipeline"]["run_as"] = {"user_name": "someone@example.com"}
    failures = check_deploy.evaluate(ROOT, declared, ids, live)
    assert any("run_as" in f for f in failures), failures


def test_a_schedule_running_when_the_repository_says_paused_is_caught():
    declared, ids, live = _state()
    live["energy_daily_pipeline"]["schedule"]["pause_status"] = "UNPAUSED"
    failures = check_deploy.evaluate(ROOT, declared, ids, live)
    assert any("schedule" in f for f in failures), failures


def test_a_notebook_left_outside_this_deployment_is_caught():
    """Prod carried an orphaned user path from D-35 to D-40 with every build green."""
    declared, ids, live = _state()
    stale = "/Workspace/Users/a-human/.bundle/energy/prod/files/notebooks/00_ingest"
    live["energy_daily_pipeline"]["tasks"][0]["notebook_task"]["notebook_path"] = stale
    failures = check_deploy.evaluate(ROOT, declared, ids, live)
    assert any("outside this deployment's root" in f for f in failures), failures


def test_a_task_that_vanished_is_caught():
    declared, ids, live = _state()
    live["energy_daily_pipeline"]["tasks"].pop()
    failures = check_deploy.evaluate(ROOT, declared, ids, live)
    assert any("tasks are" in f for f in failures), failures


@pytest.mark.parametrize("field", ["quartz_cron_expression", "timezone_id"])
def test_a_silently_changed_schedule_field_is_caught(field):
    declared, ids, live = _state()
    live["energy_daily_pipeline"]["schedule"][field] = "changed"
    failures = check_deploy.evaluate(ROOT, declared, ids, live)
    assert any("schedule" in f for f in failures), failures

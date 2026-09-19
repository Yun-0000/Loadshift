"""Followable planning: no past, hard forbidden windows, replan history.

These fail on the previous soft-window / forgetful-replan behavior.
"""

from dataclasses import replace

import pytest

from loadshift.fixtures import HORIZON_STEPS, load_demo_tasks
from loadshift.planner import run_day_ahead
from loadshift.replan import replan
from loadshift.tasks import InfeasiblePlan, Task, TimeWindow, forbidden_steps, hhmm_to_step


def test_missed_at_noon_does_not_schedule_laundry_in_the_past():
    """Repro: at noon, missed dishwasher 10–12 used to place laundry 10:00–11:30."""
    tasks = load_demo_tasks()
    previous = run_day_ahead(tasks)
    result = replan(
        tasks,
        "dishwasher",
        "missed",
        "10:00",
        "12:00",
        previous=previous,
        now="12:00",
    )
    noon = hhmm_to_step("12:00")
    missed = set(range(hhmm_to_step("10:00"), hhmm_to_step("12:00")))
    for item in result["revised_plan"]["when_to_run"]:
        steps = item["steps"]
        if item["status"] == "pending":
            assert min(steps) >= noon, (item["id"], item["planned_start"], item["planned_end"])
        assert not set(steps) & missed
    assert result["now"] == "12:00"
    laundry = next(item for item in result["revised_plan"]["when_to_run"] if item["id"] == "laundry")
    assert laundry["planned_start"] != "10:00"


def test_missed_without_explicit_now_still_locks_the_past():
    tasks = load_demo_tasks()
    result = replan(tasks, "dishwasher", "missed", "10:00", "12:00")
    noon = hhmm_to_step("12:00")
    for item in result["revised_plan"]["when_to_run"]:
        if item["status"] == "pending":
            assert min(item["steps"]) >= noon, item


def test_forbidden_windows_are_hard_when_no_contiguous_gap():
    """Two 1-hour islands totaling 2h used to still place into the hole between them."""
    task = Task(
        id="dishwasher",
        name="Dishwasher",
        power_w=1800,
        duration_hours=2.0,
        original_start="16:00",
        must_finish_by="24:00",
        inconvenient_windows=[
            TimeWindow("00:00", "10:00"),
            TimeWindow("11:00", "14:00"),
            TimeWindow("15:00", "24:00"),
        ],
    )
    blocked = forbidden_steps(task, HORIZON_STEPS)
    allowed = [step for step in range(HORIZON_STEPS) if step not in blocked]
    assert allowed == [20, 21, 28, 29]
    with pytest.raises(InfeasiblePlan) as err:
        run_day_ahead([task])
    payload = err.value.payload
    assert payload["error"] == "infeasible"
    assert payload["task_id"] == "dishwasher"
    assert any("10:00" in item or "inconvenient" in item for item in payload["relax"])
    assert "2h" in payload["message"] or "2.0h" in payload["message"]


def test_feasible_hard_window_never_uses_forbidden_slots():
    tasks = load_demo_tasks()
    plan = run_day_ahead(tasks)
    for task, planned in zip(tasks, plan.tasks, strict=True):
        blocked = forbidden_steps(task, HORIZON_STEPS)
        assert not set(planned.steps) & blocked


def test_second_replan_inherits_first_constraints_and_costs_vs_last_plan():
    tasks = load_demo_tasks()
    original = run_day_ahead(tasks)
    first = replan(
        tasks,
        "dishwasher",
        "doesnt_work",
        "10:00",
        "12:00",
        previous=original,
    )
    dish_blocked = set(range(hhmm_to_step("10:00"), hhmm_to_step("12:00")))
    first_dish = next(item for item in first["revised_plan"]["when_to_run"] if item["id"] == "dishwasher")
    assert not set(first_dish["steps"]) & dish_blocked

    second = replan(
        tasks,
        "laundry",
        "doesnt_work",
        "20:00",
        "21:00",
        previous=first["revised_plan"],
        constraint_history=first["constraint_history"],
    )
    second_dish = next(
        item for item in second["revised_plan"]["when_to_run"] if item["id"] == "dishwasher"
    )
    assert not set(second_dish["steps"]) & dish_blocked
    assert second["previous_plan"]["totals"]["bill_usd"] == first["revised_plan"]["totals"]["bill_usd"]
    assert second["previous_plan"]["totals"]["bill_usd"] != original.totals.as_dict()["bill_usd"]
    laundry_blocked = set(range(hhmm_to_step("20:00"), hhmm_to_step("21:00")))
    second_laundry = next(
        item for item in second["revised_plan"]["when_to_run"] if item["id"] == "laundry"
    )
    assert not set(second_laundry["steps"]) & laundry_blocked


def test_running_task_stays_put_and_only_future_pending_moves():
    tasks = [
        replace(task, status="running", started_at="11:00")
        if task.id == "dishwasher"
        else task
        for task in load_demo_tasks()
    ]
    plan = run_day_ahead(tasks, now="12:00")
    dish = next(item for item in plan.tasks if item.id == "dishwasher")
    assert dish.steps == list(range(hhmm_to_step("11:00"), hhmm_to_step("13:00")))
    noon = hhmm_to_step("12:00")
    for item in plan.tasks:
        if item.status == "pending":
            assert min(item.steps) >= noon


def test_completed_task_is_not_rescheduled():
    tasks = [
        replace(task, status="completed", started_at="08:00")
        if task.id == "laundry"
        else task
        for task in load_demo_tasks()
    ]
    plan = run_day_ahead(tasks, now="12:00")
    laundry = next(item for item in plan.tasks if item.id == "laundry")
    assert laundry.steps == list(range(hhmm_to_step("08:00"), hhmm_to_step("09:30")))
    assert laundry.status == "completed"


def test_partial_slot_now_and_unavailable_end_never_round_backwards():
    from loadshift.tasks import parse_now_step
    assert parse_now_step("12:01") == 25
    assert list(TimeWindow("12:10", "12:40").steps()) == [24, 25]
    assert list(TimeWindow("12:10", "12:20").steps()) == [24]
    plan = run_day_ahead(now="12:01")
    assert all(min(t.steps) >= 25 for t in plan.tasks)


def test_second_replan_cannot_rewind_clock():
    tasks = load_demo_tasks()
    first = replan(tasks, "dishwasher", "missed", "10:00", "12:00")
    second = replan(tasks, "laundry", "doesnt_work", "20:00", "21:00",
                    previous=first["revised_plan"],
                    constraint_history=first["constraint_history"], now="09:00")
    assert second["now"] == "12:00"
    assert all(min(t["steps"]) >= 24 for t in second["revised_plan"]["when_to_run"])


def test_comparison_baseline_uses_the_same_current_time():
    from loadshift.comparison import compare_day
    report = compare_day('2024-07-15', now='14:01')
    sequential = report['strategies'].get('solar_sequential')
    if sequential:
        for line in sequential['when_to_run']:
            start = line.rsplit(' ', 1)[1].split('–')[0]
            assert hhmm_to_step(start) >= 29
    else:
        assert any(f['who'] == 'solar_sequential' for f in report['failures'])

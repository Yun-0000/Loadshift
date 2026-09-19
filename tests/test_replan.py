from loadshift.fixtures import HORIZON_STEPS, load_demo_tasks
from loadshift.planner import run_day_ahead
from loadshift.replan import apply_constraint_change, replan
from loadshift.tasks import forbidden_steps, hhmm_to_step


def test_replan_moves_off_unavailable_window_and_explains_cost():
    tasks = load_demo_tasks()
    previous = run_day_ahead(tasks)
    result = replan(
        tasks,
        "dishwasher",
        "doesnt_work",
        "11:00",
        "14:00",
        previous=previous,
    )
    revised_steps = next(
        item["steps"] for item in result["revised_plan"]["when_to_run"] if item["id"] == "dishwasher"
    )
    blocked = set(range(hhmm_to_step("11:00"), hhmm_to_step("14:00")))
    assert not set(revised_steps) & blocked
    assert result["previous_when"] != result["revised_when"]
    assert "cost_of_change" in result
    assert "headline" in result["cost_of_change"]
    versus = result["cost_of_change"]["versus_previous_plan"]
    assert {"bill_usd", "grid_buy_kwh", "self_consumption_kwh"} <= set(versus)


def test_missed_window_is_recorded_as_inconvenient():
    tasks = load_demo_tasks()
    changed = apply_constraint_change(tasks, "laundry", "missed", "20:00", "21:30")
    laundry = next(task for task in changed if task.id == "laundry")
    blocked = forbidden_steps(laundry, HORIZON_STEPS)
    assert hhmm_to_step("20:00") in blocked
    assert hhmm_to_step("21:00") in blocked

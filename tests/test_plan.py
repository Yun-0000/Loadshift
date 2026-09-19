from loadshift.accounting import original_placements
from loadshift.fixtures import HORIZON_STEPS, load_demo_tasks
from loadshift.planner import run_day_ahead
from loadshift.tasks import forbidden_steps


def test_day_ahead_plan_is_followable_and_respects_deadlines():
    tasks = load_demo_tasks()
    plan = run_day_ahead(tasks)
    assert plan.solver_status.startswith("Optimal")
    assert len(plan.tasks) == len(tasks)
    by_id = {item.id: item for item in plan.tasks}
    for task in tasks:
        planned = by_id[task.id]
        assert planned.steps, task.id
        assert planned.planned_start < planned.planned_end
        blocked = forbidden_steps(task, HORIZON_STEPS)
        assert not set(planned.steps) & blocked
        assert "Run " in planned.when_to_run


def test_deltas_versus_original_are_countable():
    plan = run_day_ahead()
    assert "bill_usd" in plan.versus_original
    assert "grid_buy_kwh" in plan.versus_original
    assert "self_consumption_kwh" in plan.versus_original
    assert plan.versus_original["bill_usd"] < 0
    assert plan.totals.bill_usd < plan.original_totals.bill_usd
    assert plan.totals.grid_buy_kwh <= plan.original_totals.grid_buy_kwh + 1e-6
    assert plan.totals.self_consumption_kwh >= plan.original_totals.self_consumption_kwh - 1e-6
    assert "Estimate" in plan.method


def test_original_placements_match_stated_starts():
    tasks = load_demo_tasks()
    placed = original_placements(tasks, HORIZON_STEPS)
    assert placed["dishwasher"][0] == 38  # 19:00
    assert len(placed["laundry"]) == 3
    assert placed["ev"][0] == 36  # 18:00

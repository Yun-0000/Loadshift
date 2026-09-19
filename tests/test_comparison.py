from loadshift.accounting import noon_placements
from loadshift.baseline import solar_sequential_placements
from loadshift.comparison import (
    TIE_BILL_USD,
    TIE_GRID_KWH,
    compare_day,
    compare_days,
    verdict_vs_noon,
)
from loadshift.fixtures import (
    COMPARISON_DATES,
    COMPARISON_WINDOW,
    DATA_LABELS,
    HORIZON_STEPS,
    load_demo_tasks,
    load_forecast_day,
)
from loadshift.tasks import forbidden_steps, hhmm_to_step


def test_noon_rule_stacks_every_task_at_twelve():
    tasks = load_demo_tasks()
    placed = noon_placements(tasks, HORIZON_STEPS)
    noon = hhmm_to_step("12:00")
    for task in tasks:
        assert placed[task.id][0] == noon
        assert len(placed[task.id]) == task.duration_steps()
    # Naive stack: dishwasher and EV both occupy 12:00.
    assert placed["dishwasher"][0] == placed["ev"][0]


def test_solar_sequential_respects_the_same_hard_constraints():
    tasks = load_demo_tasks()
    day = load_forecast_day(date="2024-07-15")
    placed = solar_sequential_placements(tasks, day.frame["pv_w"].tolist(), HORIZON_STEPS)
    occupied: set[int] = set()
    for task in tasks:
        steps = placed[task.id]
        blocked = forbidden_steps(task, HORIZON_STEPS)
        assert not set(steps) & blocked
        assert not set(steps) & occupied
        occupied.update(steps)
        assert len(steps) == task.duration_steps()
        assert steps == list(range(steps[0], steps[0] + len(steps)))


def test_comparison_window_is_pre_fixed_consecutive_days():
    assert COMPARISON_WINDOW[0] == "2024-07-08"
    assert COMPARISON_WINDOW[-1] == "2024-07-21"
    assert len(COMPARISON_WINDOW) == 14
    assert COMPARISON_WINDOW == tuple(sorted(COMPARISON_WINDOW))


def test_multi_day_comparison_reports_distribution_and_labels():
    report = compare_days()
    assert report["dates"] == list(COMPARISON_WINDOW)
    assert report["window"]["n_days"] == 14
    assert "cherry-pick" not in report["summary"]["note"].lower()
    assert "change the scenario" not in report["summary"]["note"].lower()
    assert DATA_LABELS["carbon"] in report["summary"]["note"]
    assert report["data_labels"]["weather"].lower().startswith("historical")
    assert "simulated" in report["data_labels"]["household_load"].lower()
    assert "approximate" in report["data_labels"]["prices"].lower()
    assert "not proven real carbon" in report["data_labels"]["carbon"].lower()
    dist = report["distribution"]["bill_usd_vs_solar_sequential"]
    assert dist["n"] + len(report["failures"]) >= 1
    if dist["n"]:
        assert {"min", "median", "p90", "max", "mean"} <= set(dist)
    for row in report["days"]:
        assert "solar_sequential" in row["strategies"] or row["failures"]
        assert "loadshift" in row["strategies"] or row["failures"]
        assert "original" in row["strategies"]
        if row.get("verdict_vs_baseline"):
            assert row["verdict_vs_baseline"] in {"win", "tie", "lose"}
    assert "analysis" in report["summary"]
    assert "pre-fixed" in report["summary"]["analysis"].lower() or "window" in report["summary"]["analysis"].lower()


def test_demo_harvest_days_still_include_overcast_tie_vs_noon():
    report = compare_days(dates=COMPARISON_DATES)
    days = {row["date"]: row for row in report["days"]}
    feb = days["2024-02-05"]
    versus = feb["loadshift_versus_noon"]
    tiny = abs(versus["bill_usd"]) <= TIE_BILL_USD and abs(versus["grid_buy_kwh"]) <= TIE_GRID_KWH
    assert feb["verdict_vs_noon"] in {"tie", "lose"}
    assert tiny or feb["verdict_vs_noon"] == "lose"


def test_overcast_day_is_tiny_or_zero_vs_noon():
    row = compare_day("2024-02-05")
    versus = row["loadshift_versus_noon"]
    tiny = abs(versus["bill_usd"]) <= TIE_BILL_USD and abs(versus["grid_buy_kwh"]) <= TIE_GRID_KWH
    assert row["verdict_vs_noon"] in {"tie", "lose"}
    assert tiny or row["verdict_vs_noon"] == "lose"
    day = load_forecast_day(date="2024-02-05")
    assert day.frame["ghi_wm2"].max() < 200


def test_verdict_thresholds():
    from loadshift.accounting import EnergyTotals

    def totals(bill, grid):
        return EnergyTotals(bill, grid, 0, 0, 0, 0, 0)

    assert verdict_vs_noon(totals(1.00, 10.0), totals(1.02, 10.05)) == "tie"
    assert verdict_vs_noon(totals(1.00, 10.0), totals(1.20, 10.0)) == "win"
    assert verdict_vs_noon(totals(1.20, 10.0), totals(1.00, 10.0)) == "lose"

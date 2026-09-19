"""Original vs constraint-respecting solar-sequential vs LoadShift.

Comparison uses a pre-fixed consecutive window. If LoadShift has no advantage,
say so and analyze why — do not switch days or cherry-pick.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loadshift.accounting import (
    METHOD_NOTE,
    EnergyTotals,
    deltas,
    noon_placements,
    original_placements,
    score_strategy,
)
from loadshift.baseline import solar_sequential_placements, tariff_greedy_placements
from loadshift.fixtures import (
    COMPARISON_DATES,
    COMPARISON_WINDOW,
    DATA_LABELS,
    HORIZON_STEPS,
    ForecastDay,
    load_demo_tasks,
    load_forecast_day,
)
from loadshift.planner import PlanResult, run_day_ahead
from loadshift.tasks import InfeasiblePlan, Task, step_to_hhmm, parse_now_step

# Tiny/zero band vs a baseline. Inside this band we call the day a tie, not a win.
TIE_BILL_USD = 0.05
TIE_GRID_KWH = 0.15


@dataclass
class StrategyRow:
    name: str
    totals: EnergyTotals
    when_to_run: list[str]
    respects_constraints: bool
    label: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "totals": self.totals.as_dict(),
            "when_to_run": self.when_to_run,
            "respects_constraints": self.respects_constraints,
            "label": self.label,
        }


def _when_from_placements(tasks: list[Task], placements: dict[str, list[int]]) -> list[str]:
    lines = []
    for task in tasks:
        steps = placements[task.id]
        start = step_to_hhmm(steps[0])
        end = step_to_hhmm(steps[-1] + 1)
        lines.append(f"Run {task.name} {start}–{end}")
    return lines


def verdict_vs_noon(loadshift: EnergyTotals, noon: EnergyTotals) -> str:
    return verdict_vs_totals(loadshift, noon)


def verdict_vs_totals(candidate: EnergyTotals, baseline: EnergyTotals) -> str:
    bill = candidate.bill_usd - baseline.bill_usd
    grid = candidate.grid_buy_kwh - baseline.grid_buy_kwh
    if abs(bill) <= TIE_BILL_USD and abs(grid) <= TIE_GRID_KWH:
        return "tie"
    if bill < 0:
        return "win"
    return "lose"


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return round(ordered[low] * (1.0 - weight) + ordered[high] * weight, 6)


def _distribution(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    mean = sum(values) / len(values)
    return {
        "n": len(values),
        "min": round(min(values), 4),
        "p10": _percentile(values, 10),
        "median": _percentile(values, 50),
        "p90": _percentile(values, 90),
        "max": round(max(values), 4),
        "mean": round(mean, 4),
    }


def analyze_window(days: list[dict], failures: list[dict]) -> str:
    scored = [row for row in days if row.get("verdict_vs_tariff_greedy")]
    if not scored:
        return "No jointly scored days on the fixed window; see individual failures."
    outcomes = {key: sum(row["verdict_vs_tariff_greedy"] == key for row in scored) for key in ("win", "tie", "lose")}
    median = _percentile([row["loadshift_versus_tariff_greedy"]["bill_usd"] for row in scored], 50)
    return (f"Pre-fixed window {days[0]['date']}–{days[-1]['date']}: "
            f"{outcomes['win']} win / {outcomes['tie']} tie / {outcomes['lose']} lose vs tariff-aware greedy "
            f"(median Δbill ${median:.2f}). {len(failures)} strategy failure(s). "
            "Small or zero differences are retained. " + DATA_LABELS["carbon"])


def compare_day(
    date: str,
    tasks: list[Task] | None = None,
    fixtures_dir: Path | None = None,
    day: ForecastDay | None = None,
    loadshift_plan: PlanResult | None = None,
    now: str | None = None,
) -> dict:
    day = day or load_forecast_day(fixtures_dir, date=date)
    tasks = list(tasks or load_demo_tasks(fixtures_dir))
    horizon = len(day.frame)
    if horizon != HORIZON_STEPS:
        raise ValueError(f"Fixture horizon must be {HORIZON_STEPS} steps")

    original = original_placements(tasks, horizon)
    noon = noon_placements(tasks, horizon)
    original_totals = score_strategy(day.frame, tasks, original)
    noon_totals = score_strategy(day.frame, tasks, noon)

    failures: list[dict] = []
    sequential = None
    sequential_totals = None
    try:
        sequential = solar_sequential_placements(
            tasks, day.frame["pv_w"].tolist(), horizon, now_step=parse_now_step(now)
        )
        sequential_totals = score_strategy(day.frame, tasks, sequential)
    except InfeasiblePlan as exc:
        failures.append(
            {"date": date, "who": "solar_sequential", "reason": str(exc), "relax": exc.relax}
        )

    greedy = greedy_totals = None
    try:
        greedy = tariff_greedy_placements(tasks, day.frame, now_step=parse_now_step(now))
        greedy_totals = score_strategy(day.frame, tasks, greedy)
    except InfeasiblePlan as exc:
        failures.append({"date": date, "who": "tariff_greedy", "reason": str(exc), "relax": exc.relax})

    plan = None
    loadshift_totals = None
    try:
        plan = loadshift_plan or run_day_ahead(
            tasks, fixtures_dir, date=date, day=day, now=now
        )
        loadshift_totals = plan.totals
    except InfeasiblePlan as exc:
        failures.append(
            {"date": date, "who": "loadshift", "reason": str(exc), "relax": exc.relax}
        )

    strategies = {
        "original": StrategyRow(
            "original",
            original_totals,
            _when_from_placements(tasks, original),
            False,
            "Original evening arrangement (what the household would have done).",
        ).as_dict(),
        "noon": StrategyRow(
            "noon",
            noon_totals,
            _when_from_placements(tasks, noon),
            False,
            "Weak baseline: stack every task at noon. Does not respect living constraints.",
        ).as_dict(),
    }
    if sequential is not None and sequential_totals is not None:
        strategies["solar_sequential"] = StrategyRow(
            "solar_sequential",
            sequential_totals,
            _when_from_placements(tasks, sequential),
            True,
            "Constraint-respecting baseline: sequential placement in highest-PV legal windows.",
        ).as_dict()
    if greedy is not None:
        strategies["tariff_greedy"] = StrategyRow(
            "tariff_greedy", greedy_totals, _when_from_placements(tasks, greedy), True,
            "Tariff-aware greedy: overlapping jobs, same constraints and 9 kW import cap; all orders up to five pending jobs.",
        ).as_dict()
    if plan is not None and loadshift_totals is not None:
        strategies["loadshift"] = StrategyRow(
            "loadshift",
            loadshift_totals,
            [task.when_to_run for task in plan.tasks],
            True,
            "LoadShift plan under the same hard constraints.",
        ).as_dict()

    versus_noon = (
        deltas(noon_totals, loadshift_totals).as_dict() if loadshift_totals is not None else None
    )
    versus_original = (
        deltas(original_totals, loadshift_totals).as_dict()
        if loadshift_totals is not None
        else None
    )
    versus_baseline = (
        deltas(sequential_totals, loadshift_totals).as_dict()
        if loadshift_totals is not None and sequential_totals is not None
        else None
    )
    outcome_noon = (
        verdict_vs_totals(loadshift_totals, noon_totals) if loadshift_totals is not None else None
    )
    outcome_baseline = (
        verdict_vs_totals(loadshift_totals, sequential_totals)
        if loadshift_totals is not None and sequential_totals is not None
        else None
    )

    return {
        "date": date,
        "label": day.location.get("name"),
        "pv_kwh": round(float(day.frame["pv_w"].sum() * 0.5 / 1000.0), 2),
        "ghi_max": round(float(day.frame["ghi_wm2"].max()), 1),
        "strategies": strategies,
        "loadshift_versus_tariff_greedy": deltas(greedy_totals, loadshift_totals).as_dict() if greedy_totals is not None and loadshift_totals is not None else None,
        "verdict_vs_tariff_greedy": verdict_vs_totals(loadshift_totals, greedy_totals) if greedy_totals is not None and loadshift_totals is not None else None,
        "loadshift_versus_original": versus_original,
        "loadshift_versus_noon": versus_noon,
        "loadshift_versus_baseline": versus_baseline,
        "verdict_vs_noon": outcome_noon,
        "verdict_vs_baseline": outcome_baseline,
        "failures": failures,
        "solver_status": plan.solver_status if plan is not None else None,
        "method": METHOD_NOTE,
        "data_labels": DATA_LABELS,
    }


def compare_days(
    dates: tuple[str, ...] | list[str] | None = None,
    tasks: list[Task] | None = None,
    fixtures_dir: Path | None = None,
    now: str | None = None,
) -> dict:
    dates = tuple(dates or COMPARISON_WINDOW)
    tasks = list(tasks or load_demo_tasks(fixtures_dir))
    days = [
        compare_day(date, tasks=tasks, fixtures_dir=fixtures_dir, now=now) for date in dates
    ]
    failures = [item for row in days for item in row["failures"]]
    scored = [row for row in days if row.get("verdict_vs_baseline")]
    wins = [row["date"] for row in scored if row["verdict_vs_baseline"] == "win"]
    ties = [row["date"] for row in scored if row["verdict_vs_baseline"] == "tie"]
    losses = [row["date"] for row in scored if row["verdict_vs_baseline"] == "lose"]
    noon_wins = [row["date"] for row in days if row.get("verdict_vs_noon") == "win"]
    noon_ties = [row["date"] for row in days if row.get("verdict_vs_noon") == "tie"]
    noon_losses = [row["date"] for row in days if row.get("verdict_vs_noon") == "lose"]
    bills = [
        row["loadshift_versus_baseline"]["bill_usd"]
        for row in scored
        if row.get("loadshift_versus_baseline")
    ]
    grid = [
        row["loadshift_versus_baseline"]["grid_buy_kwh"]
        for row in scored
        if row.get("loadshift_versus_baseline")
    ]
    selfc = [
        row["loadshift_versus_baseline"]["self_consumption_kwh"]
        for row in scored
        if row.get("loadshift_versus_baseline")
    ]
    return {
        "dates": list(dates),
        "window": {"start": dates[0], "end": dates[-1], "n_days": len(dates)},
        "days": days,
        "failures": failures,
        "distribution": {
            "bill_usd_vs_tariff_greedy": _distribution([row["loadshift_versus_tariff_greedy"]["bill_usd"] for row in days if row["loadshift_versus_tariff_greedy"] is not None]),
            "bill_usd_vs_solar_sequential": _distribution(bills),
            "grid_buy_kwh_vs_solar_sequential": _distribution(grid),
            "self_consumption_kwh_vs_solar_sequential": _distribution(selfc),
        },
        "summary": {
            "vs_tariff_greedy": {outcome: [row["date"] for row in days if row["verdict_vs_tariff_greedy"] == outcome] for outcome in ("win", "tie", "lose")},
            "win": wins,
            "tie": ties,
            "lose": losses,
            "vs_noon_weak_baseline": {"win": noon_wins, "tie": noon_ties, "lose": noon_losses},
            "default_date": dates[0],
            "default_verdict_vs_baseline": scored[0]["verdict_vs_baseline"] if scored else None,
            "default_verdict_vs_noon": days[0].get("verdict_vs_noon"),
            "analysis": analyze_window(days, failures),
            "note": (
                "Primary baseline is tariff-aware greedy with overlap and the same import cap. Solar-sequential adds a no-overlap restriction. "
                "Noon is a weak unconstrained stack, kept only as a labeled contrast. "
                "The window is pre-fixed; days are not swapped after seeing results. "
                + DATA_LABELS["carbon"]
            ),
        },
        "data_labels": DATA_LABELS,
        "method": METHOD_NOTE,
        # Kept for the Remotion harvest which still locks the three demo days.
        "demo_harvest_dates": list(COMPARISON_DATES),
    }

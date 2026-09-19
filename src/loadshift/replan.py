"""Replan when a suggested time does not work or was missed."""

from __future__ import annotations

from dataclasses import replace

from loadshift.accounting import EnergyTotals, explain_cost_of_change, score_strategy
from loadshift.fixtures import ForecastDay, load_forecast_day
from loadshift.planner import PlanResult, run_day_ahead
from loadshift.tasks import Task, TimeWindow, parse_now_step, step_to_hhmm


def apply_constraint_change(
    tasks: list[Task],
    task_id: str,
    reason: str,
    unavailable_start: str | None = None,
    unavailable_end: str | None = None,
) -> list[Task]:
    """Return a new task list with the living-constraint change applied."""
    reason_key = reason.strip().lower()
    if reason_key not in {"doesnt_work", "doesn't work", "missed", "time_does_not_work"}:
        raise ValueError("reason must be 'doesnt_work' or 'missed'")
    if not unavailable_start or not unavailable_end:
        raise ValueError("unavailable_start and unavailable_end are required (HH:MM)")

    window = TimeWindow(start=unavailable_start, end=unavailable_end)
    updated: list[Task] = []
    found = False
    for task in tasks:
        if task.id != task_id:
            updated.append(task)
            continue
        found = True
        windows = list(task.inconvenient_windows)
        if not any(item.start == window.start and item.end == window.end for item in windows):
            windows.append(window)
        updated.append(replace(task, inconvenient_windows=windows))
    if not found:
        raise ValueError(f"Unknown task id {task_id!r}")
    return updated


def _apply_history(tasks: list[Task], history: list[dict] | None) -> list[Task]:
    current = list(tasks)
    for item in history or []:
        current = apply_constraint_change(
            current,
            item["task_id"],
            item.get("reason") or "doesnt_work",
            item["unavailable_start"],
            item["unavailable_end"],
        )
    return current


def _resolve_now(reason: str, unavailable_end: str, now: str | None) -> str | None:
    """Missed windows have already passed: lock the past at least through the window end."""
    if reason.strip().lower() != "missed":
        return now
    implied_step = parse_now_step(unavailable_end)
    given_step = parse_now_step(now)
    return step_to_hhmm(max(implied_step, given_step))


def _totals_from_plan(plan: PlanResult | dict) -> EnergyTotals:
    if isinstance(plan, PlanResult):
        return plan.totals
    payload = plan["totals"]
    return EnergyTotals(
        bill_usd=float(payload["bill_usd"]),
        grid_buy_kwh=float(payload["grid_buy_kwh"]),
        grid_sell_kwh=float(payload.get("grid_sell_kwh") or 0),
        self_consumption_kwh=float(payload["self_consumption_kwh"]),
        self_consumption_pct=float(payload.get("self_consumption_pct") or 0),
        pv_kwh=float(payload.get("pv_kwh") or 0),
        load_kwh=float(payload.get("load_kwh") or 0),
    )


def _rows_from_plan(plan: PlanResult | dict) -> list[dict]:
    if isinstance(plan, PlanResult):
        return [task.as_dict() for task in plan.tasks]
    return list(plan["when_to_run"])


def _plan_as_dict(plan: PlanResult | dict) -> dict:
    if isinstance(plan, PlanResult):
        return plan.as_dict()
    return plan


def validated_previous(previous: dict, tasks: list[Task], day: ForecastDay) -> dict:
    """Recalculate the prior bill from validated placements, never client totals."""
    rows = previous.get('when_to_run')
    if not isinstance(rows, list) or len(rows) != len(tasks):
        raise ValueError('previous_plan must contain one scheduled row per task')
    placements = {}
    by_id = {t.id: t for t in tasks}
    for row in rows:
        if not isinstance(row, dict) or row.get('id') not in by_id or row['id'] in placements:
            raise ValueError('previous_plan contains unknown or duplicate tasks')
        task, steps = by_id[row['id']], row.get('steps')
        if (not isinstance(steps, list) or len(steps) != task.duration_steps()
                or any(type(s) is not int or not 0 <= s < len(day.frame) for s in steps)
                or steps != list(range(steps[0], steps[0] + len(steps)))):
            raise ValueError('previous_plan must contain valid continuous task placements')
        if row.get('planned_start') != step_to_hhmm(steps[0]):
            raise ValueError('previous_plan start does not match its placement')
        placements[task.id] = steps
    parse_now_step(previous.get('now'))
    return {**previous, 'totals': score_strategy(day.frame, tasks, placements).as_dict()}


def _change_rows(previous: PlanResult | dict, revised: PlanResult, task_id: str, why: str) -> list[dict]:
    before = {row["id"]: row for row in _rows_from_plan(previous)}
    changes = []
    for task in revised.tasks:
        prior = before.get(task.id) or {}
        if prior.get("steps") != task.steps or prior.get("planned_start") != task.planned_start:
            changes.append(
                {
                    "id": task.id,
                    "name": task.name,
                    "from": prior.get("when_to_run") or prior.get("planned_start"),
                    "to": task.when_to_run,
                    "why": why if task.id == task_id else "Moved so the rest of the day stays followable.",
                }
            )
    return changes


def replan(
    tasks: list[Task],
    task_id: str,
    reason: str,
    unavailable_start: str,
    unavailable_end: str,
    previous: PlanResult | dict | None = None,
    constraint_history: list[dict] | None = None,
    now: str | None = None,
    day: ForecastDay | None = None,
) -> dict:
    day = day or load_forecast_day()
    if isinstance(previous, dict):
        previous = validated_previous(previous, tasks, day)
    # A later request must not rewind time or forget a previously missed window.
    prior_now = previous.now if isinstance(previous, PlanResult) else (previous or {}).get("now")
    clock_steps = [parse_now_step(now), parse_now_step(prior_now)]
    clock_steps.extend(
        parse_now_step(item["unavailable_end"])
        for item in constraint_history or []
        if item.get("reason", "").strip().lower() == "missed"
    )
    if max(clock_steps):
        now = step_to_hhmm(max(clock_steps))
    now = _resolve_now(reason, unavailable_end, now)
    inherited = _apply_history(list(tasks), constraint_history)
    previous = previous or run_day_ahead(inherited, now=now, day=day)
    revised_tasks = apply_constraint_change(
        inherited, task_id, reason, unavailable_start, unavailable_end
    )
    revised = run_day_ahead(revised_tasks, arrangement="replan", now=now, day=day)
    cost = explain_cost_of_change(_totals_from_plan(previous), revised.totals)
    reason_text = (
        "that time was missed"
        if reason.strip().lower() == "missed"
        else "that time does not work"
    )
    moved = next(item for item in revised.tasks if item.id == task_id)
    before_rows = _rows_from_plan(previous)
    before = next(item for item in before_rows if item["id"] == task_id)
    history = list(constraint_history or [])
    history.append(
        {
            "task_id": task_id,
            "reason": reason,
            "unavailable_start": unavailable_start,
            "unavailable_end": unavailable_end,
        }
    )
    why = (
        f"{moved.name}: {reason_text} {unavailable_start}–{unavailable_end}"
        + (f"; current time {now}" if now else "")
    )
    return {
        "reason": reason_text,
        "task_id": task_id,
        "unavailable": {"start": unavailable_start, "end": unavailable_end},
        "now": now,
        "previous_when": before.get("when_to_run"),
        "revised_when": moved.when_to_run,
        "changes": _change_rows(previous, revised, task_id, why),
        "cost_of_change": cost,
        "constraint_history": history,
        "revised_tasks": [task.as_dict() for task in revised_tasks],
        "previous_plan": _plan_as_dict(previous),
        "revised_plan": revised.as_dict(),
    }

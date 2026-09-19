"""Constraint-respecting baselines. Not a carbon claim."""

from __future__ import annotations

from itertools import permutations

import numpy as np
import pandas as pd

from loadshift.tasks import (
    InfeasiblePlan,
    Task,
    diagnose_task,
    forbidden_steps,
    locked_placement,
)


def solar_sequential_placements(
    tasks: list[Task],
    pv_w: list[float],
    horizon: int,
    now_step: int = 0,
) -> dict[str, list[int]]:
    """Place pending tasks one-after-another in the highest-PV legal windows.

    Same hard constraints as LoadShift: past locked, forbidden windows, deadlines.
    Tasks do not overlap. Completed / running blocks stay put.
    """
    occupied: set[int] = set()
    placements: dict[str, list[int]] = {}
    pending: list[Task] = []
    for task in tasks:
        locked = locked_placement(task, horizon)
        if locked is not None:
            placements[task.id] = locked
            occupied.update(locked)
        else:
            pending.append(task)

    pending.sort(
        key=lambda task: (
            _deadline_step(task),
            -task.duration_steps(),
            task.id,
        )
    )
    for task in pending:
        duration = task.duration_steps()
        blocked = forbidden_steps(task, horizon, now_step=now_step)
        best: list[int] | None = None
        best_score = float("-inf")
        for start in range(0, horizon - duration + 1):
            block = list(range(start, start + duration))
            if any(step in blocked or step in occupied for step in block):
                continue
            score = sum(pv_w[step] for step in block)
            if score > best_score:
                best_score = score
                best = block
        if best is None:
            detail = diagnose_task(task, horizon, now_step=now_step) or {}
            extra = " Sequential placement also refuses to overlap other tasks."
            raise InfeasiblePlan(
                (detail.get("message") or f"Cannot place {task.name} sequentially.") + extra,
                relax=detail.get("relax") or [],
                task_id=task.id,
            )
        placements[task.id] = best
        occupied.update(best)
    return placements


def _deadline_step(task: Task) -> int:
    from loadshift.tasks import hhmm_to_step

    return hhmm_to_step(task.must_finish_by)


def tariff_greedy_placements(
    tasks: list[Task], frame: pd.DataFrame, now_step: int = 0,
) -> dict[str, list[int]]:
    """Minimize marginal bill per job, allowing overlap and a 9 kW grid import.

    Try every task order for up to five pending jobs; above that use input,
    deadline and descending-energy orders. This is a bounded heuristic, not
    an optimal solver. Finished and running jobs keep their assigned blocks.
    """
    horizon = len(frame)
    pv = frame.pv_w.to_numpy()
    load = frame.load_w.to_numpy().copy()
    buy = frame.import_usd_per_kwh.to_numpy()
    sell = frame.export_usd_per_kwh.to_numpy()
    fixed, pending = {}, []
    for task in tasks:
        block = locked_placement(task, horizon)
        if block is None:
            pending.append(task)
        else:
            fixed[task.id] = block
            load[block] += task.power_w

    def bill(total):
        net = total - pv
        return float((np.maximum(net, 0) * buy - np.maximum(-net, 0) * sell).sum() * .0005)

    orders = permutations(pending) if len(pending) <= 5 else (
        pending, sorted(pending, key=_deadline_step),
        sorted(pending, key=lambda t: -t.power_w * t.duration_steps()),
    )
    best = None
    for order in orders:
        total, placed = load.copy(), dict(fixed)
        for task in order:
            duration = task.duration_steps()
            blocked = forbidden_steps(task, horizon, now_step=now_step)
            choices = []
            for start in range(horizon - duration + 1):
                block = list(range(start, start + duration))
                if blocked.intersection(block):
                    continue
                candidate = total.copy()
                candidate[block] += task.power_w
                if np.any(candidate - pv > 9000 + 1e-6):
                    continue
                choices.append((bill(candidate), start, candidate, block))
            if not choices:
                break
            _, _, total, placed[task.id] = min(choices, key=lambda item: item[:2])
        else:
            value = bill(total)
            if best is None or value < best[0]:
                best = value, placed
    if best is None:
        raise InfeasiblePlan("Tariff-aware greedy could not place all jobs in its bounded search.")
    return best[1]

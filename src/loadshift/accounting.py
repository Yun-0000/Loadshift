"""Apples-to-apples energy accounting for original vs LoadShift vs a replan.

Method (visible, not fake precision):
  For each 30-minute slot:
    total_w = household_load_w + sum(deferrable_w)
    self_consumed_w = min(total_w, pv_w)
    grid_buy_w = max(total_w - pv_w, 0)
    grid_sell_w = max(pv_w - total_w, 0)
    bill_usd += (grid_buy_kwh * import_price) - (grid_sell_kwh * export_price)

Totals are estimates from the sourced fixtures and the published-style TOU.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from loadshift.fixtures import STEP_MINUTES
from loadshift.tasks import Task


@dataclass
class EnergyTotals:
    bill_usd: float
    grid_buy_kwh: float
    grid_sell_kwh: float
    self_consumption_kwh: float
    self_consumption_pct: float
    pv_kwh: float
    load_kwh: float

    def as_dict(self) -> dict:
        return {
            "bill_usd": round(self.bill_usd, 2),
            "grid_buy_kwh": round(self.grid_buy_kwh, 2),
            "grid_sell_kwh": round(self.grid_sell_kwh, 2),
            "self_consumption_kwh": round(self.self_consumption_kwh, 2),
            "self_consumption_pct": round(self.self_consumption_pct, 1),
            "pv_kwh": round(self.pv_kwh, 2),
            "load_kwh": round(self.load_kwh, 2),
        }


@dataclass
class Deltas:
    bill_usd: float
    grid_buy_kwh: float
    self_consumption_kwh: float
    method: str

    def as_dict(self) -> dict:
        return {
            "bill_usd": round(self.bill_usd, 2),
            "grid_buy_kwh": round(self.grid_buy_kwh, 2),
            "self_consumption_kwh": round(self.self_consumption_kwh, 2),
            "method": self.method,
            "reading": {
                "bill_usd": "negative = LoadShift cheaper than the comparison",
                "grid_buy_kwh": "negative = less grid energy imported",
                "self_consumption_kwh": "positive = more PV used on-site",
            },
        }


HOURS = STEP_MINUTES / 60.0
METHOD_NOTE = (
    "Estimate from sourced 30-min fixtures: bill = grid-buy*import − grid-sell*export; "
    "self-consumption = min(house+tasks, PV). Not a live utility bill."
)


def deferrable_power_series(
    index: pd.DatetimeIndex, tasks: list[Task], placements: dict[str, list[int]]
) -> pd.DataFrame:
    frame = pd.DataFrame(0.0, index=index, columns=[task.id for task in tasks])
    for task in tasks:
        for step in placements[task.id]:
            frame.iloc[step, frame.columns.get_loc(task.id)] = task.power_w
    return frame


def original_placements(tasks: list[Task], horizon: int) -> dict[str, list[int]]:
    return {task.id: task.original_steps(horizon) for task in tasks}


def noon_placements(
    tasks: list[Task], horizon: int, noon_start: str = "12:00"
) -> dict[str, list[int]]:
    """Naive rule: start every deferrable load at noon, stacked.

    Tasks overlap on purpose. That is the 'shift everything to noon' baseline,
    not a feasible living-constraint plan.
    """
    from loadshift.tasks import hhmm_to_step

    start = hhmm_to_step(noon_start)
    placed: dict[str, list[int]] = {}
    for task in tasks:
        length = task.duration_steps()
        if start + length > horizon:
            start_here = max(0, horizon - length)
        else:
            start_here = start
        placed[task.id] = list(range(start_here, start_here + length))
    return placed


def score_strategy(
    frame: pd.DataFrame,
    tasks: list[Task],
    placements: dict[str, list[int]],
) -> EnergyTotals:
    deferrable = deferrable_power_series(frame.index, tasks, placements)
    return totals_from_series(
        frame["load_w"],
        frame["pv_w"],
        frame["import_usd_per_kwh"],
        frame["export_usd_per_kwh"],
        deferrable,
    )


def totals_from_series(
    load_w: pd.Series,
    pv_w: pd.Series,
    import_price: pd.Series,
    export_price: pd.Series,
    deferrable_w: pd.DataFrame,
) -> EnergyTotals:
    total_w = load_w + deferrable_w.sum(axis=1)
    self_w = pd.concat([total_w, pv_w], axis=1).min(axis=1)
    buy_w = (total_w - pv_w).clip(lower=0)
    sell_w = (pv_w - total_w).clip(lower=0)
    buy_kwh = buy_w * HOURS / 1000.0
    sell_kwh = sell_w * HOURS / 1000.0
    self_kwh = self_w * HOURS / 1000.0
    pv_kwh = float((pv_w * HOURS / 1000.0).sum())
    load_kwh = float((total_w * HOURS / 1000.0).sum())
    bill = float((buy_kwh * import_price - sell_kwh * export_price).sum())
    pct = 100.0 * float(self_kwh.sum()) / pv_kwh if pv_kwh else 0.0
    return EnergyTotals(
        bill_usd=bill,
        grid_buy_kwh=float(buy_kwh.sum()),
        grid_sell_kwh=float(sell_kwh.sum()),
        self_consumption_kwh=float(self_kwh.sum()),
        self_consumption_pct=pct,
        pv_kwh=pv_kwh,
        load_kwh=load_kwh,
    )


def deltas(comparison: EnergyTotals, candidate: EnergyTotals) -> Deltas:
    return Deltas(
        bill_usd=candidate.bill_usd - comparison.bill_usd,
        grid_buy_kwh=candidate.grid_buy_kwh - comparison.grid_buy_kwh,
        self_consumption_kwh=candidate.self_consumption_kwh - comparison.self_consumption_kwh,
        method=METHOD_NOTE,
    )


def explain_cost_of_change(previous: EnergyTotals, revised: EnergyTotals) -> dict:
    change = deltas(previous, revised)
    bill = change.bill_usd
    if abs(bill) < 0.01 and abs(change.grid_buy_kwh) < 0.05:
        headline = "The change is effectively free on this day’s fixtures."
    elif bill > 0:
        headline = (
            f"This change costs about ${bill:.2f} more than the previous LoadShift plan "
            f"and buys {change.grid_buy_kwh:+.2f} kWh from the grid."
        )
    else:
        headline = (
            f"This change is about ${-bill:.2f} cheaper than the previous LoadShift plan "
            f"({change.grid_buy_kwh:+.2f} kWh grid-buy, "
            f"{change.self_consumption_kwh:+.2f} kWh self-consumption)."
        )
    return {
        "headline": headline,
        "versus_previous_plan": change.as_dict(),
        "previous": previous.as_dict(),
        "revised": revised.as_dict(),
    }

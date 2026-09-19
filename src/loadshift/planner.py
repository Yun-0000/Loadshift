"""Plan household tasks against day-ahead forecasts and hard constraints."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

import cvxpy as cp
import numpy as np
import pandas as pd
import pytz

from loadshift.optimizer.optimization import Optimization
from loadshift.accounting import (
    EnergyTotals,
    METHOD_NOTE,
    deferrable_power_series,
    deltas,
    original_placements,
    totals_from_series,
)
from loadshift.fixtures import (
    DEMO_DATE,
    DEMO_TZ,
    HORIZON_STEPS,
    STEP_MINUTES,
    ForecastDay,
    load_demo_tasks,
    load_forecast_day,
)
from loadshift.paths import OPTIMIZER_DIR
from loadshift.tasks import (
    InfeasiblePlan,
    Task,
    allowed_steps,
    forbidden_steps,
    locked_placement,
    parse_now_step,
    require_feasible,
    step_to_hhmm,
    steps_are_contiguous,
    window_mask,
)

LOGGER = logging.getLogger("loadshift")


@dataclass
class PlannedTask:
    id: str
    name: str
    power_w: float
    duration_hours: float
    original_start: str
    planned_start: str
    planned_end: str
    steps: list[int]
    when_to_run: str
    status: str = "pending"
    started_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "power_w": self.power_w,
            "duration_hours": self.duration_hours,
            "original_start": self.original_start,
            "planned_start": self.planned_start,
            "planned_end": self.planned_end,
            "steps": self.steps,
            "when_to_run": self.when_to_run,
            "status": self.status,
            "started_at": self.started_at,
        }


@dataclass
class PlanResult:
    arrangement: str
    tasks: list[PlannedTask]
    totals: EnergyTotals
    original_totals: EnergyTotals
    versus_original: dict
    solver_status: str
    fixture: dict
    method: str
    now: str | None = None
    timeline: dict | None = None

    def as_dict(self) -> dict:
        return {
            "arrangement": self.arrangement,
            "when_to_run": [task.as_dict() for task in self.tasks],
            "totals": self.totals.as_dict(),
            "original_totals": self.original_totals.as_dict(),
            "versus_original": self.versus_original,
            "solver_status": self.solver_status,
            "fixture": self.fixture,
            "method": self.method,
            "now": self.now,
            "timeline": self.timeline,
        }


def _retrieve_hass_conf() -> dict:
    return {
        "optimization_time_step": pd.to_timedelta(STEP_MINUTES, "minutes"),
        "time_zone": pytz.timezone(DEMO_TZ),
        "sensor_power_photovoltaics": "sensor.power_photovoltaics",
        "sensor_power_load_no_var_loads": "sensor.power_load_no_var_loads",
        "historic_days_to_retrieve": 2,
        "method_ts_round": "nearest",
        "load_negative": False,
        "set_zero_min": True,
        "sensor_replace_zero": ["sensor.power_photovoltaics"],
        "sensor_linear_interp": [
            "sensor.power_photovoltaics",
            "sensor.power_load_no_var_loads",
        ],
    }


def _plant_conf() -> dict:
    return {
        "maximum_power_from_grid": 9000,
        "maximum_power_to_grid": 9000,
        "inverter_is_hybrid": False,
        "compute_curtailment": False,
        "pv_module_model": [
            "CSUN_Eurasia_Energy_Systems_Industry_and_Trade_CSUN295_60M"
        ],
        "pv_inverter_model": [5000],
        "surface_tilt": [30],
        "surface_azimuth": [180],
        "modules_per_string": [16],
        "strings_per_inverter": [1],
        "inverter_ac_output_max": 5000,
        "inverter_ac_input_max": 5000,
        "inverter_efficiency_dc_ac": 1.0,
        "inverter_efficiency_ac_dc": 1.0,
        "inverter_stress_cost": 0.0,
        "inverter_stress_segments": 10,
        "number_of_batteries": 1,
        "battery_discharge_power_max": 1000,
        "battery_charge_power_max": 1000,
        "battery_charge_power_derating": [],
        "battery_discharge_efficiency": 0.95,
        "battery_charge_efficiency": 0.95,
        "battery_nominal_energy_capacity": 5000,
        "battery_minimum_state_of_charge": 0.3,
        "battery_maximum_state_of_charge": 0.9,
        "battery_target_state_of_charge": 0.6,
        "battery_stress_cost": 0.0,
        "battery_stress_segments": 10,
    }


def _optim_conf(tasks: list[Task], horizon: int, now_step: int = 0) -> dict:
    n = len(tasks)
    starts: list[int] = []
    ends: list[int] = []
    hours: list[float] = []
    powers: list[float] = []
    cost_overrides: list[list[float]] = []
    for task in tasks:
        allowed = allowed_steps(task, horizon, now_step=now_step)
        starts.append(allowed[0])
        # LoadShift: end=0 means unrestricted; non-zero is the first disallowed step.
        # Bounding box of allowed steps; holes are zeroed by the hard window mask.
        ends.append(allowed[-1] + 1)
        hours.append(task.duration_hours)
        powers.append(task.power_w)
        cost_overrides.append([0.0] * horizon)
    return {
        "costfun": "profit",
        "set_use_pv": True,
        "set_use_battery": False,
        "set_use_adjusted_pv": False,
        "delta_forecast_daily": 1,
        "number_of_deferrable_loads": n,
        "nominal_power_of_deferrable_loads": powers,
        "minimum_power_of_deferrable_loads": [0.0] * n,
        "operating_hours_of_each_deferrable_load": hours,
        "start_timesteps_of_each_deferrable_load": starts,
        "end_timesteps_of_each_deferrable_load": ends,
        "treat_deferrable_load_as_semi_cont": [True] * n,
        "set_deferrable_load_single_constant": [True] * n,
        "set_deferrable_startup_penalty": [0.0] * n,
        "set_deferrable_max_startups": [1] * n,
        "def_minimum_on_time": [0] * n,
        "def_minimum_off_time": [0] * n,
        "def_load_config": [{} for _ in tasks],
        "is_electric_load": [True] * n,
        "cost_forecast_per_deferrable_load": cost_overrides,
        "deferrable_load_max_cost": [0.0] * n,
        "deferrable_load_groups": [],
        "set_total_pv_sell": False,
        "set_nocharge_from_grid": False,
        "set_nodischarge_to_grid": True,
        "set_battery_first_priority": False,
        "set_battery_dynamic": False,
        "battery_dynamic_max": 0.9,
        "battery_dynamic_min": -0.9,
        "weight_battery_discharge": 0.0,
        "weight_battery_charge": 0.0,
        "lp_solver": "Highs",
        "lp_solver_timeout": 45,
        "lp_solver_mip_rel_gap": 0.0,
        "num_threads": 1,
        "capacity_cost_per_kw": 0.0,
        "capacity_charge_interval_timesteps": 1,
        "load_peak_hours_cost": 0.52,
        "load_offpeak_hours_cost": 0.28,
        "photovoltaic_production_sell_price": 0.08,
    }


def _apply_tariff_to_overrides(optim_conf: dict, import_prices: list[float]) -> None:
    filled = []
    for _override in optim_conf["cost_forecast_per_deferrable_load"]:
        filled.append(list(import_prices))
    optim_conf["cost_forecast_per_deferrable_load"] = filled


def _extract_run_steps(power: pd.Series, threshold_w: float = 50.0) -> list[int]:
    return [int(i) for i, value in enumerate(power.tolist()) if value > threshold_w]


def _planned_task(task: Task, steps: list[int]) -> PlannedTask:
    if not steps:
        raise ValueError(f"LoadShift did not schedule {task.name}")
    start = step_to_hhmm(steps[0])
    end = step_to_hhmm(steps[-1] + 1)
    suffix = f" (was {task.original_start})"
    if task.status == "running":
        suffix = f" (running, started {task.started_at or task.original_start})"
    elif task.status == "completed":
        suffix = f" (already ran, started {task.started_at or task.original_start})"
    return PlannedTask(
        id=task.id,
        name=task.name,
        power_w=task.power_w,
        duration_hours=task.duration_hours,
        original_start=task.original_start,
        planned_start=start,
        planned_end=end,
        steps=steps,
        when_to_run=f"Run {task.name} {start}–{end}{suffix}",
        status=task.status,
        started_at=task.started_at,
    )


@contextmanager
def _hard_window_masks(opt: Optimization, masks: list[np.ndarray]):
    """Intersect the optimizer's contiguous window with hard allowed slots.

    The base time window encodes a single [start, end] interval. Forbidden holes
    inside that interval are applied through the household constraint mask. The AND happens immediately before each solve,
    including the relaxed-LP fallback.
    """
    real_solve = cp.Problem.solve

    def wrapped(problem, *args, **kwargs):
        for index, allowed in enumerate(masks):
            param = opt.param_window_masks[index]
            current = param.value
            if current is None:
                param.value = np.asarray(allowed, dtype=float)
            else:
                param.value = np.asarray(current, dtype=float) * np.asarray(allowed, dtype=float)
        return real_solve(problem, *args, **kwargs)

    cp.Problem.solve = wrapped  # type: ignore[method-assign]
    try:
        yield
    finally:
        cp.Problem.solve = real_solve  # type: ignore[method-assign]


def _timeline(day: ForecastDay, planned: list[PlannedTask], tasks: list[Task]) -> dict:
    frame = day.frame
    by_id = {task.id: task for task in tasks}
    return {
        "step_minutes": STEP_MINUTES,
        "times": [step_to_hhmm(step) for step in range(len(frame))],
        "pv_w": [round(float(v), 1) for v in frame["pv_w"].tolist()],
        "load_w": [round(float(v), 1) for v in frame["load_w"].tolist()],
        "tasks": [
            {
                "id": item.id,
                "name": item.name,
                "steps": item.steps,
                "planned_start": item.planned_start,
                "planned_end": item.planned_end,
                "status": item.status,
                "inconvenient_windows": [
                    w.as_dict() for w in by_id[item.id].inconvenient_windows
                ],
            }
            for item in planned
        ],
    }


def _split_locked(tasks: list[Task], horizon: int) -> tuple[list[Task], dict[str, list[int]]]:
    pending: list[Task] = []
    locked: dict[str, list[int]] = {}
    for task in tasks:
        placement = locked_placement(task, horizon)
        if placement is None:
            pending.append(task)
        else:
            locked[task.id] = placement
    return pending, locked


def _assert_followable(
    pending: list[Task],
    placements: dict[str, list[int]],
    horizon: int,
    now_step: int,
) -> None:
    for task in pending:
        steps = placements[task.id]
        blocked = forbidden_steps(task, horizon, now_step=now_step)
        hit = sorted(set(steps) & blocked)
        if hit:
            raise InfeasiblePlan(
                f"{task.name} was placed into a forbidden window "
                f"({step_to_hhmm(hit[0])}–{step_to_hhmm(hit[-1] + 1)}). "
                "Forbidden windows are hard constraints.",
                relax=None if not hit else [
                    f"unavailable/inconvenient or past slot at {step_to_hhmm(s)}"
                    for s in hit[:4]
                ],
                task_id=task.id,
            )
        if not steps_are_contiguous(steps):
            raise InfeasiblePlan(
                f"{task.name} was split across gaps; a followable plan must be one block.",
                task_id=task.id,
            )


# ponytail: one process-wide solver patch; serialize solves until masks become instance-local.
_SOLVE_LOCK = RLock()


def run_day_ahead(
    tasks: list[Task] | None = None,
    fixtures_dir: Path | None = None,
    arrangement: str = "loadshift",
    date: str | None = None,
    day: ForecastDay | None = None,
    now: str | None = None,
    rolling: bool = False,
) -> PlanResult:
    day = day or load_forecast_day(fixtures_dir, date=date or DEMO_DATE)
    tasks = list(tasks or load_demo_tasks(fixtures_dir))
    frame = day.frame
    horizon = len(frame)
    if horizon != HORIZON_STEPS:
        raise ValueError(f"Fixture horizon must be {HORIZON_STEPS} steps")
    now_step = parse_now_step(now)

    require_feasible(tasks, horizon, now_step=now_step)
    pending, locked = _split_locked(tasks, horizon)

    placements: dict[str, list[int]] = dict(locked)
    status = "locked"

    if pending:
        optim_conf = _optim_conf(pending, horizon, now_step=now_step)
        _apply_tariff_to_overrides(optim_conf, frame["import_usd_per_kwh"].tolist())

        optimizer_config = {
            "data_path": OPTIMIZER_DIR / "data",
            "root_path": OPTIMIZER_DIR,
            "defaults_path": OPTIMIZER_DIR / "data" / "config_defaults.json",
            "associations_path": OPTIMIZER_DIR / "data" / "associations.csv",
        }
        logger = logging.getLogger("loadshift.optimizer")
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.WARNING)

        opt = Optimization(
            _retrieve_hass_conf(),
            optim_conf,
            _plant_conf(),
            "import_usd_per_kwh",
            "export_usd_per_kwh",
            "profit",
            optimizer_config,
            logger,
            opt_time_delta=24,
            num_timesteps=horizon,
        )
        masks = [window_mask(task, horizon, now_step=now_step) for task in pending]
        # Committed jobs still consume power and must affect the next decision.
        committed = deferrable_power_series(
            frame.index, [task for task in tasks if task.id in locked], locked
        ).sum(axis=1)
        optimization_load = frame["load_w"] + committed
        with _SOLVE_LOCK, _hard_window_masks(opt, masks):
            if rolling:
                result = opt.perform_naive_mpc_optim(
                    frame, frame["pv_w"], optimization_load, prediction_horizon=horizon
                )
            else:
                result = opt.perform_dayahead_forecast_optim(
                    frame, frame["pv_w"], optimization_load
                )
        status = (
            str(result["optim_status"].iloc[0]) if "optim_status" in result.columns else "unknown"
        )
        if "Optimal" not in status or "Relaxed" in status:
            raise InfeasiblePlan(
                f"LoadShift could not return a followable integer plan (status {status!r}). "
                "A deadline, unavailable window, or current-time lock must be relaxed.",
            )

        for index, task in enumerate(pending):
            column = f"P_deferrable{index}"
            if column not in result.columns:
                raise RuntimeError(f"LoadShift result missing {column}")
            steps = _extract_run_steps(result[column], threshold_w=task.power_w * 0.5)
            placements[task.id] = steps

        _assert_followable(pending, placements, horizon, now_step)

    planned = [_planned_task(task, placements[task.id]) for task in tasks]
    deferrable = deferrable_power_series(frame.index, tasks, placements)
    original = deferrable_power_series(frame.index, tasks, original_placements(tasks, horizon))
    loadshift_totals = totals_from_series(
        frame["load_w"],
        frame["pv_w"],
        frame["import_usd_per_kwh"],
        frame["export_usd_per_kwh"],
        deferrable,
    )
    original_totals = totals_from_series(
        frame["load_w"],
        frame["pv_w"],
        frame["import_usd_per_kwh"],
        frame["export_usd_per_kwh"],
        original,
    )
    return PlanResult(
        arrangement=arrangement,
        tasks=planned,
        totals=loadshift_totals,
        original_totals=original_totals,
        versus_original=deltas(original_totals, loadshift_totals).as_dict(),
        solver_status=status,
        fixture={
            "location": day.location,
            "method": day.method,
            "sources": day.provenance.get("sources", {}),
            "step_minutes": STEP_MINUTES,
            "data_labels": day.method.get("data_labels"),
        },
        method=METHOD_NOTE,
        now=now,
        timeline=_timeline(day, planned, tasks),
    )

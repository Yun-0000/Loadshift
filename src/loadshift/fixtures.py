"""Sourced demo forecasts. No live secrets. See fixtures/provenance.json."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from loadshift.paths import FIXTURES_DIR
from loadshift.tasks import Task, TimeWindow, task_from_dict

STEP_MINUTES = 30
HORIZON_STEPS = 48
DEMO_DATE = "2024-07-15"
DEMO_TZ = "America/Los_Angeles"
# Three sourced days used by the demo video harvest. The product comparison
# uses COMPARISON_WINDOW (consecutive), not these cherry-picked days.
COMPARISON_DATES = ("2024-07-15", "2024-06-20", "2024-02-05")
COMPARISON_WINDOW_START = "2024-07-08"
COMPARISON_WINDOW_END = "2024-07-21"
COMPARISON_WINDOW_RAW = "openmeteo_la_2024-07-08_2024-07-21.json"
NOON_START = "12:00"

DATA_LABELS = {
    "weather": "Historical weather: Open-Meteo ERA5 archive snapshots. Not a live forecast.",
    "household_load": (
        "Simulated household load: typical residential weekday shape (~28 kWh/day). "
        "Not a metered home."
    ),
    "prices": (
        "Approximate prices: rounded PG&E E-TOU-C-like summer weekday structure. "
        "Not a live utility bill."
    ),
    "carbon": (
        "Figures are fixture-based bill, grid-buy, and self-consumption estimates. "
        "They are not proven real carbon reductions."
    ),
}

DEVICE_PRESETS = [
    {
        "id": "dishwasher",
        "name": "Dishwasher",
        "power_w": 1800,
        "duration_hours": 2.0,
        "original_start": "19:00",
        "must_finish_by": "22:00",
        "inconvenient_windows": [{"start": "21:00", "end": "24:00"}],
    },
    {
        "id": "laundry",
        "name": "Clothes washer",
        "power_w": 1000,
        "duration_hours": 1.5,
        "original_start": "20:00",
        "must_finish_by": "22:00",
        "inconvenient_windows": [{"start": "07:00", "end": "09:00"}],
    },
    {
        "id": "dryer",
        "name": "Clothes dryer",
        "power_w": 3000,
        "duration_hours": 1.0,
        "original_start": "20:30",
        "must_finish_by": "22:00",
        "inconvenient_windows": [],
    },
    {
        "id": "ev",
        "name": "EV charge (Level 2)",
        "power_w": 3300,
        "duration_hours": 3.0,
        "original_start": "18:00",
        "must_finish_by": "23:00",
        "inconvenient_windows": [{"start": "22:00", "end": "24:00"}],
    },
    {
        "id": "ev_l1",
        "name": "EV charge (Level 1)",
        "power_w": 1400,
        "duration_hours": 6.0,
        "original_start": "18:00",
        "must_finish_by": "24:00",
        "inconvenient_windows": [],
    },
    {
        "id": "pool",
        "name": "Pool pump",
        "power_w": 1200,
        "duration_hours": 4.0,
        "original_start": "18:00",
        "must_finish_by": "22:00",
        "inconvenient_windows": [],
    },
]

# PVWatts-style rooftop, documented in provenance.json.
PV_STC_W = 6000.0
PV_TEMP_COEFF = -0.004
PV_SYSTEM_DERATE = 0.86
PV_NOCT_DELTA = 25.0  # °C rise at 1000 W/m² (simple NOCT-style cell temp)

# Rounded public PG&E E-TOU-C-like summer weekday structure (not a live API).
TOU_PEAK_USD_PER_KWH = 0.52
TOU_OFFPEAK_USD_PER_KWH = 0.28
EXPORT_USD_PER_KWH = 0.08
TOU_PEAK_START = 16
TOU_PEAK_END = 21


@dataclass(frozen=True)
class ForecastDay:
    frame: pd.DataFrame
    provenance: dict
    location: dict
    method: dict


def load_provenance(fixtures_dir: Path | None = None) -> dict:
    path = (fixtures_dir or FIXTURES_DIR) / "provenance.json"
    return json.loads(path.read_text())


def load_demo_tasks(fixtures_dir: Path | None = None) -> list[Task]:
    path = (fixtures_dir or FIXTURES_DIR) / "demo_tasks.json"
    payload = json.loads(path.read_text())
    return [task_from_dict(item) for item in payload["tasks"]]


def _interpolate_hourly(hourly_times: list[str], values: list[float]) -> list[float]:
    """Hold each hour at :00, linearly interpolate the :30 slot toward the next hour."""
    half_hours: list[float] = []
    for index, value in enumerate(values):
        half_hours.append(float(value))
        nxt = values[index + 1] if index + 1 < len(values) else value
        half_hours.append(float(value) * 0.5 + float(nxt) * 0.5)
    if len(half_hours) != HORIZON_STEPS:
        raise ValueError(f"Expected {HORIZON_STEPS} half-hours, got {len(half_hours)}")
    return half_hours


def _pv_from_weather(ghi_wm2: list[float], temp_c: list[float]) -> list[float]:
    """PVWatts-style AC power from GHI and air temperature."""
    out: list[float] = []
    for ghi, temp in zip(ghi_wm2, temp_c, strict=True):
        t_cell = temp + (ghi / 1000.0) * PV_NOCT_DELTA
        temp_factor = 1.0 + PV_TEMP_COEFF * (t_cell - 25.0)
        power = PV_STC_W * (max(ghi, 0.0) / 1000.0) * temp_factor * PV_SYSTEM_DERATE
        out.append(max(power, 0.0))
    return out


def _typical_summer_load_w() -> list[float]:
    """30-min household load (W), non-deferrable only.

    Shape follows the published OpenEI / NREL residential summer-weekday
    diurnal pattern: night baseload, morning bump, midday cooling, evening peak.
    Scaled to about 28 kWh/day for a typical 3-bed SoCal home. This is a
    documented typical shape, not a metered house.
    """
    # 48 half-hour factors, then scaled. Evening peak > midday so shifting
    # deferrable work into solar hours is countable.
    shape = [
        0.45, 0.42, 0.40, 0.40, 0.39, 0.39, 0.40, 0.42,  # 00:00-04:00
        0.48, 0.55, 0.70, 0.90, 1.10, 1.20, 1.05, 0.95,  # 04:00-08:00
        0.85, 0.80, 0.78, 0.82, 0.90, 0.98, 1.05, 1.10,  # 08:00-12:00
        1.15, 1.18, 1.20, 1.22, 1.25, 1.28, 1.32, 1.40,  # 12:00-16:00
        1.55, 1.70, 1.85, 1.95, 2.05, 2.10, 2.00, 1.80,  # 16:00-20:00
        1.50, 1.25, 1.00, 0.80, 0.65, 0.55, 0.50, 0.47,  # 20:00-24:00
    ]
    # Mean shape ~1.0 * 1.15 kW * 24h ≈ 27.6 kWh.
    return [round(factor * 1150.0, 1) for factor in shape]


def _import_price(step: int) -> float:
    hour = (step * STEP_MINUTES) // 60
    if TOU_PEAK_START <= hour < TOU_PEAK_END:
        return TOU_PEAK_USD_PER_KWH
    return TOU_OFFPEAK_USD_PER_KWH


def comparison_window_dates() -> tuple[str, ...]:
    start = date.fromisoformat(COMPARISON_WINDOW_START)
    end = date.fromisoformat(COMPARISON_WINDOW_END)
    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(days)


COMPARISON_WINDOW = comparison_window_dates()


def raw_weather_path(date: str, fixtures_dir: Path | None = None) -> Path:
    return (fixtures_dir or FIXTURES_DIR) / "raw" / f"openmeteo_la_{date}.json"


def comparison_window_path(fixtures_dir: Path | None = None) -> Path:
    return (fixtures_dir or FIXTURES_DIR) / "raw" / COMPARISON_WINDOW_RAW


def _hourly_for_date(raw: dict, day: str) -> dict:
    times = raw["hourly"]["time"]
    indexes = [i for i, stamp in enumerate(times) if str(stamp).startswith(day)]
    if len(indexes) != 24:
        raise ValueError(f"Expected 24 hourly rows for {day}, got {len(indexes)}")
    return {key: [raw["hourly"][key][i] for i in indexes] for key in raw["hourly"]}


def load_raw_weather(date: str, fixtures_dir: Path | None = None) -> dict:
    root = fixtures_dir or FIXTURES_DIR
    single = raw_weather_path(date, root)
    if single.is_file():
        return json.loads(single.read_text())
    window = comparison_window_path(root)
    if window.is_file():
        raw = json.loads(window.read_text())
        return {**raw, "hourly": _hourly_for_date(raw, date)}
    raise FileNotFoundError(f"Missing sourced weather snapshot for {date}")


def load_forecast_day(fixtures_dir: Path | None = None, date: str = DEMO_DATE) -> ForecastDay:
    root = fixtures_dir or FIXTURES_DIR
    provenance = load_provenance(root)
    raw = load_raw_weather(date, root)
    hourly = raw["hourly"]
    ghi = _interpolate_hourly(hourly["time"], hourly["shortwave_radiation"])
    temp = _interpolate_hourly(hourly["time"], hourly["temperature_2m"])
    cloud = _interpolate_hourly(hourly["time"], hourly["cloud_cover"])
    pv = _pv_from_weather(ghi, temp)
    load = _typical_summer_load_w()
    index = pd.date_range(
        f"{date} 00:00",
        periods=HORIZON_STEPS,
        freq=f"{STEP_MINUTES}min",
        tz=DEMO_TZ,
    )
    frame = pd.DataFrame(
        {
            "ghi_wm2": ghi,
            "temp_c": temp,
            "cloud_cover_pct": cloud,
            "pv_w": pv,
            "load_w": load,
            "import_usd_per_kwh": [_import_price(step) for step in range(HORIZON_STEPS)],
            "export_usd_per_kwh": [EXPORT_USD_PER_KWH] * HORIZON_STEPS,
        },
        index=index,
    )
    return ForecastDay(
        frame=frame,
        provenance=provenance,
        location={
            "name": "Los Angeles, CA (representative US solar home)",
            "latitude": raw["latitude"],
            "longitude": raw["longitude"],
            "timezone": raw["timezone"],
            "date": date,
        },
        method={
            "weather": f"Open-Meteo archive snapshot for {date} (ERA5-backed hourly GHI/temperature/cloud)",
            "pv": (
                f"PVWatts-style: {PV_STC_W:.0f} W STC * (GHI/1000) * "
                f"(1{PV_TEMP_COEFF:+}*(Tcell-25)) * {PV_SYSTEM_DERATE} derate"
            ),
            "load": "OpenEI/NREL typical residential summer-weekday shape, ~28 kWh/day, deferrables excluded",
            "prices": (
                f"PG&E E-TOU-C-like summer weekday: peak {TOU_PEAK_START:02d}:00-"
                f"{TOU_PEAK_END:02d}:00 ${TOU_PEAK_USD_PER_KWH}/kWh, else "
                f"${TOU_OFFPEAK_USD_PER_KWH}/kWh, export ${EXPORT_USD_PER_KWH}/kWh"
            ),
            "precision": "Estimates from sourced fixtures and a published-style tariff. Not a live utility bill.",
            "data_labels": DATA_LABELS,
        },
    )


def write_derived_csvs(fixtures_dir: Path | None = None) -> Path:
    root = fixtures_dir or FIXTURES_DIR
    day = load_forecast_day(root)
    out = root / "day_ahead.csv"
    export = day.frame.copy()
    export.index.name = "timestamp"
    export.to_csv(out)
    return out


def default_demo_tasks() -> list[Task]:
    """Built-in demo if demo_tasks.json is missing — same objects as the fixture file."""
    return [
        Task(
            id="dishwasher",
            name="Dishwasher",
            power_w=1800,
            duration_hours=2.0,
            original_start="19:00",
            must_finish_by="22:00",
            inconvenient_windows=[TimeWindow("21:00", "24:00")],
        ),
        Task(
            id="laundry",
            name="Clothes washer",
            power_w=1000,
            duration_hours=1.5,
            original_start="20:00",
            must_finish_by="22:00",
            inconvenient_windows=[TimeWindow("07:00", "09:00")],
        ),
        Task(
            id="ev",
            name="EV charge (Level 2)",
            power_w=3300,
            duration_hours=3.0,
            original_start="18:00",
            must_finish_by="23:00",
            inconvenient_windows=[TimeWindow("22:00", "24:00")],
        ),
    ]

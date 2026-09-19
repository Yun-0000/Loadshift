from __future__ import annotations

from dataclasses import asdict, dataclass, field

VALID_STATUSES = {"pending", "running", "completed"}


class InfeasiblePlan(ValueError):
    """Hard constraints leave no followable block for one or more tasks."""

    def __init__(
        self,
        message: str,
        relax: list[str] | None = None,
        task_id: str | None = None,
    ):
        super().__init__(message)
        self.relax = list(relax or [])
        self.task_id = task_id
        self.payload = {
            "error": "infeasible",
            "message": message,
            "relax": self.relax,
            "task_id": task_id,
        }


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if hour == 24 and minute == 0:
        return 24, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid clock time {value!r}")
    return hour, minute


def hhmm_to_step(value: str, step_minutes: int = 30) -> int:
    hour, minute = parse_hhmm(value)
    minutes = hour * 60 + minute
    if minutes == 24 * 60:
        return (24 * 60) // step_minutes
    return minutes // step_minutes


def step_to_hhmm(step: int, step_minutes: int = 30) -> str:
    minutes = step * step_minutes
    if minutes >= 24 * 60:
        return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_now_step(now: str | None, step_minutes: int = 30) -> int:
    if not now:
        return 0
    hour, minute = parse_hhmm(now)
    return (hour * 60 + minute + step_minutes - 1) // step_minutes


@dataclass
class TimeWindow:
    start: str
    end: str

    def steps(self, step_minutes: int = 30) -> range:
        start = hhmm_to_step(self.start, step_minutes)
        end = parse_now_step(self.end, step_minutes)
        if parse_hhmm(self.end) <= parse_hhmm(self.start):
            raise ValueError(f"Window end must be after start: {self.start}-{self.end}")
        return range(start, end)

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end}


@dataclass
class Task:
    id: str
    name: str
    power_w: float
    duration_hours: float
    original_start: str
    must_finish_by: str
    inconvenient_windows: list[TimeWindow] = field(default_factory=list)
    status: str = "pending"
    started_at: str | None = None

    def duration_steps(self, step_minutes: int = 30) -> int:
        steps = int(round(self.duration_hours * 60 / step_minutes))
        if abs(steps * step_minutes - self.duration_hours * 60) > 1e-6:
            raise ValueError(f"Task {self.id} duration must use {step_minutes}-minute increments")
        if steps < 1:
            raise ValueError(f"Task {self.id} duration is shorter than one timestep")
        return steps

    def original_steps(self, horizon: int, step_minutes: int = 30) -> list[int]:
        start = hhmm_to_step(self.original_start, step_minutes)
        length = self.duration_steps(step_minutes)
        steps = [start + offset for offset in range(length)]
        if steps[-1] >= horizon:
            raise ValueError(
                f"Task {self.id} original start {self.original_start} plus "
                f"{self.duration_hours}h exceeds the day horizon"
            )
        return steps

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "power_w": self.power_w,
            "duration_hours": self.duration_hours,
            "original_start": self.original_start,
            "must_finish_by": self.must_finish_by,
            "inconvenient_windows": [window.as_dict() for window in self.inconvenient_windows],
            "status": self.status,
            "started_at": self.started_at,
        }


def task_from_dict(raw: dict) -> Task:
    windows = [
        TimeWindow(start=item["start"], end=item["end"])
        for item in raw.get("inconvenient_windows", [])
    ]
    status = str(raw.get("status") or "pending").strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"Task status must be one of {sorted(VALID_STATUSES)}, got {status!r}")
    started_at = raw.get("started_at")
    started_at = str(started_at) if started_at else None
    return Task(
        id=str(raw["id"]),
        name=str(raw["name"]),
        power_w=float(raw["power_w"]),
        duration_hours=float(raw["duration_hours"]),
        original_start=str(raw["original_start"]),
        must_finish_by=str(raw["must_finish_by"]),
        inconvenient_windows=windows,
        status=status,
        started_at=started_at,
    )


def tasks_from_payload(payload: list[dict]) -> list[Task]:
    tasks = [task_from_dict(item) for item in payload]
    if not tasks:
        raise ValueError("At least one task is required")
    seen = [task.id for task in tasks]
    if len(seen) != len(set(seen)):
        raise ValueError("Task ids must be unique")
    return tasks


def forbidden_steps(
    task: Task,
    horizon: int,
    step_minutes: int = 30,
    now_step: int = 0,
) -> set[int]:
    """Timesteps the load must not run: past, after the deadline, or forbidden windows.

    Forbidden / inconvenient windows are hard constraints. Past slots are locked
    for pending tasks so a replan at noon cannot place work at 10:00.
    """
    blocked: set[int] = set()
    if now_step > 0:
        for step in range(min(now_step, horizon)):
            blocked.add(step)
    deadline = hhmm_to_step(task.must_finish_by, step_minutes)
    for step in range(max(deadline, 0), horizon):
        blocked.add(step)
    for window in task.inconvenient_windows:
        for step in window.steps(step_minutes):
            if 0 <= step < horizon:
                blocked.add(step)
    return blocked


def allowed_steps(
    task: Task,
    horizon: int,
    step_minutes: int = 30,
    now_step: int = 0,
) -> list[int]:
    blocked = forbidden_steps(task, horizon, step_minutes, now_step=now_step)
    return [step for step in range(horizon) if step not in blocked]


def contiguous_runs(steps: list[int]) -> list[list[int]]:
    if not steps:
        return []
    ordered = sorted(steps)
    runs = [[ordered[0]]]
    for step in ordered[1:]:
        if step == runs[-1][-1] + 1:
            runs[-1].append(step)
        else:
            runs.append([step])
    return runs


def longest_contiguous_len(steps: list[int]) -> int:
    runs = contiguous_runs(steps)
    return max((len(run) for run in runs), default=0)


def steps_are_contiguous(steps: list[int]) -> bool:
    if not steps:
        return False
    ordered = sorted(steps)
    return ordered == list(range(ordered[0], ordered[0] + len(ordered)))


def constraint_labels(task: Task, now_step: int = 0, step_minutes: int = 30) -> list[str]:
    labels: list[str] = []
    if now_step > 0:
        labels.append(f"current time {step_to_hhmm(now_step, step_minutes)} locks all earlier slots")
    for window in task.inconvenient_windows:
        labels.append(f"unavailable/inconvenient {window.start}–{window.end}")
    labels.append(f"must finish by {task.must_finish_by}")
    return labels


def diagnose_task(
    task: Task,
    horizon: int,
    step_minutes: int = 30,
    now_step: int = 0,
) -> dict | None:
    """Return an infeasibility payload if no single followable block fits."""
    duration = task.duration_steps(step_minutes)
    allowed = allowed_steps(task, horizon, step_minutes, now_step=now_step)
    longest = longest_contiguous_len(allowed)
    if longest >= duration:
        return None
    relax = constraint_labels(task, now_step=now_step, step_minutes=step_minutes)
    gap_h = longest * step_minutes / 60.0
    message = (
        f"Cannot place {task.name} ({task.duration_hours:g}h) as one followable block. "
        f"Longest free gap is {gap_h:g}h; need {task.duration_hours:g}h. "
        f"Relax one of: {'; '.join(relax)}."
    )
    return {
        "task_id": task.id,
        "message": message,
        "relax": relax,
        "longest_gap_hours": gap_h,
    }


def require_feasible(
    tasks: list[Task],
    horizon: int,
    step_minutes: int = 30,
    now_step: int = 0,
) -> None:
    pending = [task for task in tasks if locked_placement(task, horizon, step_minutes) is None]
    problems = [
        diagnose_task(task, horizon, step_minutes, now_step=now_step) for task in pending
    ]
    problems = [item for item in problems if item]
    if not problems:
        return
    if len(problems) == 1:
        item = problems[0]
        raise InfeasiblePlan(item["message"], relax=item["relax"], task_id=item["task_id"])
    lines = [item["message"] for item in problems]
    relax: list[str] = []
    for item in problems:
        relax.extend(item["relax"])
    raise InfeasiblePlan(
        "Several tasks have no followable block.\n" + "\n".join(lines),
        relax=list(dict.fromkeys(relax)),
    )


def locked_placement(
    task: Task, horizon: int, step_minutes: int = 30
) -> list[int] | None:
    """Completed and running tasks keep the block they already started. None = pending."""
    if task.status == "pending":
        return None
    start_s = task.started_at or task.original_start
    start = hhmm_to_step(start_s, step_minutes)
    length = task.duration_steps(step_minutes)
    steps = list(range(start, start + length))
    if not steps or steps[-1] >= horizon:
        raise ValueError(
            f"Task {task.id} ({task.status}) starting {start_s} plus "
            f"{task.duration_hours}h exceeds the day horizon"
        )
    return steps


def window_mask(task: Task, horizon: int, now_step: int = 0, step_minutes: int = 30):
    import numpy as np

    allowed = allowed_steps(task, horizon, step_minutes, now_step=now_step)
    mask = np.zeros(horizon, dtype=float)
    for step in allowed:
        mask[step] = 1.0
    return mask


def task_to_payload(task: Task) -> dict:
    return task.as_dict()


def constraint_change_dict(task_id: str, reason: str, start: str, end: str) -> dict:
    return {
        "task_id": task_id,
        "reason": reason,
        "unavailable_start": start,
        "unavailable_end": end,
    }


def asdict_safe(obj) -> dict:
    return asdict(obj)

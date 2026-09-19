from __future__ import annotations

from contextlib import asynccontextmanager
import os
import time
from fastapi import FastAPI, HTTPException, Request
from urllib.parse import urlparse

from loadshift.controller import configured_controller
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from loadshift.comparison import compare_days
from loadshift.fixtures import (
    DATA_LABELS,
    DEVICE_PRESETS,
    STEP_MINUTES,
    load_demo_tasks,
    load_forecast_day,
    load_provenance,
)
from loadshift.paths import WEB_DIR
from loadshift.planner import run_day_ahead
from loadshift.replan import replan
from loadshift.tasks import InfeasiblePlan, Task, step_to_hhmm, tasks_from_payload

@asynccontextmanager
async def lifespan(app):
    if os.environ.get('LOADSHIFT_PUBLIC_DEMO') == '1':
        if len(os.environ.get('LOADSHIFT_SESSION_SECRET', '')) < 32:
            raise RuntimeError('Set a stable LOADSHIFT_SESSION_SECRET of at least 32 characters')
        yield
        return
    controller = configured_controller()
    app.state.controller = controller
    controller.run_background(interval=5 if controller.mode == 'demo' else 15)
    try:
        yield
    finally:
        controller.close()


app = FastAPI(title="LoadShift", version="0.2.0", lifespan=lifespan)


@app.middleware('http')
async def public_demo_session(request: Request, call_next):
    if os.environ.get('LOADSHIFT_PUBLIC_DEMO') != '1' or not request.url.path.startswith('/api/'):
        return await call_next(request)
    from loadshift.public_demo import COOKIE, controller_for, encode
    from fastapi.responses import JSONResponse
    secret = os.environ.get('LOADSHIFT_SESSION_SECRET', '')
    if len(secret) < 32:
        return JSONResponse({'detail': 'Demo session configuration is missing'}, status_code=503)
    controller = controller_for(request.cookies.get(COOKIE, ''), secret)
    request.state.controller = controller
    # ponytail: one accelerated tick per visible poll; demo is not a persistent home worker.
    if request.url.path == '/api/control' and time.time() - controller.state.get('last_poll', 0) >= 5:
        controller.tick(advance=True)
        controller.state['last_poll'] = time.time()
    response = await call_next(request)
    controller.state['issued'] = time.time()
    try:
        cookie = encode(controller.state, secret)
    except ValueError as exc:
        return JSONResponse({'detail': str(exc)}, status_code=400)
    response.set_cookie(COOKIE, cookie, httponly=True, secure=request.url.scheme == 'https',
                        samesite='strict', max_age=86400)
    response.headers['Cache-Control'] = 'no-store'
    return response


class WindowIn(BaseModel):
    start: str
    end: str


class TaskIn(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=100)
    power_w: float = Field(gt=0, le=20000, allow_inf_nan=False)
    duration_hours: float = Field(gt=0, le=24, allow_inf_nan=False)
    original_start: str
    must_finish_by: str
    inconvenient_windows: list[WindowIn] = Field(default_factory=list, max_length=48)
    status: str = "pending"
    started_at: str | None = None


class ConstraintIn(BaseModel):
    task_id: str
    reason: str = "doesnt_work"
    unavailable_start: str
    unavailable_end: str


class PlanRequest(BaseModel):
    tasks: list[TaskIn] | None = Field(default=None, max_length=12)
    now: str | None = None


class ReplanRequest(BaseModel):
    tasks: list[TaskIn] | None = Field(default=None, max_length=12)
    task_id: str
    reason: str = "doesnt_work"
    unavailable_start: str
    unavailable_end: str
    now: str | None = None
    previous_plan: dict | None = None
    constraint_history: list[ConstraintIn] = Field(default_factory=list, max_length=100)


def _tasks(payload: list[TaskIn] | None) -> list[Task]:
    if payload is None:
        return load_demo_tasks()
    return tasks_from_payload([item.model_dump() for item in payload])


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, InfeasiblePlan):
        return HTTPException(status_code=409, detail=exc.payload)
    return HTTPException(status_code=400, detail=str(exc))


def _horizon_payload(day) -> dict:
    frame = day.frame
    return {
        "step_minutes": STEP_MINUTES,
        "times": [step_to_hhmm(step) for step in range(len(frame))],
        "pv_w": [round(float(v), 1) for v in frame["pv_w"].tolist()],
        "load_w": [round(float(v), 1) for v in frame["load_w"].tolist()],
        "ghi_wm2": [round(float(v), 1) for v in frame["ghi_wm2"].tolist()],
    }


def _planning_context(request: Request, tasks: list[Task], now: str | None):
    if getattr(request.state, 'controller', None) is not None or getattr(request.app.state, 'controller', None) is not None:
        return _controller(request).preview(tasks, now)
    return load_forecast_day(), tasks, now


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/demo")
def demo(request: Request) -> dict:
    tasks = load_demo_tasks()
    home = None
    if getattr(request.state, 'controller', None) is not None or getattr(request.app.state, 'controller', None) is not None:
        home = _controller(request).snapshot()
        if home['tasks']:
            tasks = tasks_from_payload(home['tasks'])
    try:
        day, tasks, now = _planning_context(request, tasks, None)
    except (ValueError, RuntimeError) as exc:
        raise _http_error(exc) from exc
    return {
        'public_demo': getattr(request.state, 'controller', None) is not None,
        'home': home,
        'date': day.frame.index[0].date().isoformat(),
        'now': now,
        "tasks": [task.as_dict() for task in tasks],
        "presets": DEVICE_PRESETS,
        "horizon": _horizon_payload(day),
        "data_labels": DATA_LABELS,
        "fixture": {
            "location": day.location,
            "method": day.method,
            "sources": load_provenance().get("sources", {}),
        },
    }


@app.post("/api/plan")
def plan(req: PlanRequest, request: Request) -> dict:
    try:
        day, tasks, now = _planning_context(request, _tasks(req.tasks), req.now)
        return run_day_ahead(tasks, now=now, day=day).as_dict()
    except (ValueError, RuntimeError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/compare")
def compare_api(req: PlanRequest) -> dict:
    try:
        return compare_days(tasks=_tasks(req.tasks), now=req.now)
    except (ValueError, RuntimeError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/replan")
def replan_api(req: ReplanRequest, request: Request) -> dict:
    try:
        day, tasks, now = _planning_context(request, _tasks(req.tasks), req.now)
        history = [item.model_dump() for item in req.constraint_history]
        return replan(
            tasks,
            req.task_id,
            req.reason,
            req.unavailable_start,
            req.unavailable_end,
            previous=req.previous_plan,
            constraint_history=history,
            now=now,
            day=day,
        )
    except (ValueError, RuntimeError) as exc:
        raise _http_error(exc) from exc


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def _controller(request: Request):
    controller = getattr(request.state, 'controller', None) or getattr(request.app.state, 'controller', None)
    if controller is None:
        raise HTTPException(503, 'Controller is starting')
    # The control surface is local-only. No cross-origin device commands.
    hostname = request.url.hostname
    origin = request.headers.get('origin')
    public = getattr(request.state, 'controller', None) is not None
    if (not public and hostname not in {'127.0.0.1', 'localhost', '::1', 'testserver'}) or (
        origin and urlparse(origin).netloc != request.headers.get('host')
    ):
        raise HTTPException(403, 'Open the local LoadShift page to control this home')
    return controller


@app.get('/api/control')
def control_status(request: Request):
    return _controller(request).snapshot()


@app.post('/api/demo/reset')
def reset_demo(request: Request):
    controller = _controller(request)
    if getattr(request.state, 'controller', None) is None:
        raise HTTPException(403, 'Reset is available only in the hosted sample home')
    from loadshift.controller import Controller
    controller.state = Controller(None).state
    return {'reset': True}


@app.post('/api/control/start')
def control_start(req: PlanRequest, request: Request):
    try:
        return _controller(request).start(_tasks(req.tasks), now=req.now or '09:00')
    except (ValueError, RuntimeError) as exc:
        raise _http_error(exc) from exc


@app.post('/api/control/pause')
def control_pause(request: Request):
    return _controller(request).pause()


@app.post('/api/control/resume')
def control_resume(request: Request):
    try:
        return _controller(request).resume()
    except (ValueError, RuntimeError) as exc:
        raise _http_error(exc) from exc


class PreferencesRequest(PlanRequest):
    base_revision: int = Field(ge=0)


@app.post('/api/control/preferences')
def control_preferences(req: PreferencesRequest, request: Request):
    try:
        return _controller(request).preferences(_tasks(req.tasks), req.base_revision)
    except (ValueError, RuntimeError) as exc:
        raise _http_error(exc) from exc


class WeatherRequest(BaseModel):
    profile: str


@app.post('/api/control/weather')
def control_weather(req: WeatherRequest, request: Request):
    try:
        return _controller(request).weather(req.profile)
    except (ValueError, RuntimeError) as exc:
        raise _http_error(exc) from exc

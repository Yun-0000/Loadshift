"""Acceptance regressions: saved intent, clock boundaries and trustworthy results."""
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from loadshift.app import app
from loadshift.controller import Controller
from loadshift.fixtures import load_demo_tasks, load_forecast_day
from loadshift.planner import run_day_ahead
from loadshift.tasks import TimeWindow


def test_small_load_can_be_scheduled():
    job = replace(load_demo_tasks()[0], power_w=50)
    result = run_day_ahead([job])
    assert len(result.tasks[0].steps) == job.duration_steps()


def test_malformed_prior_plan_is_rejected_and_client_bill_not_trusted():
    client = TestClient(app)
    jobs = [t.as_dict() for t in load_demo_tasks()]
    request = dict(tasks=jobs, task_id='dishwasher', reason='doesnt_work',
                   unavailable_start='10:00', unavailable_end='12:00')
    assert client.post('/api/replan', json={**request, 'previous_plan': {'bad': True}}).status_code == 400
    prior = client.post('/api/plan', json={'tasks': jobs}).json()
    bill = prior['totals']['bill_usd']
    prior['totals']['bill_usd'] = -999999
    result = client.post('/api/replan', json={**request, 'previous_plan': prior})
    assert result.status_code == 200
    assert result.json()['previous_plan']['totals']['bill_usd'] == bill


def test_demo_rejects_clock_that_cannot_reach_scheduled_slots(tmp_path):
    c = Controller(tmp_path / 'home.json')
    try:
        with pytest.raises(ValueError, match='30'):
            c.start(load_demo_tasks(), now='09:15')
        assert not c.snapshot()['active']
    finally:
        c.close()


def test_saved_constraints_survive_refresh_and_stale_edit_is_rejected(tmp_path, monkeypatch):
    c = Controller(tmp_path / 'home.json')
    monkeypatch.setattr(app.state, 'controller', c, raising=False)
    client = TestClient(app)
    try:
        assert client.get('/api/demo').status_code == 200
        jobs = load_demo_tasks()
        jobs[0] = replace(jobs[0], inconvenient_windows=(TimeWindow('11:00', '14:00'),))
        initial = client.post('/api/control/start', json={'tasks': [t.as_dict() for t in jobs]}).json()
        client.post('/api/control/pause')
        refreshed = client.get('/api/demo').json()
        assert refreshed['tasks'][0]['inconvenient_windows'] == [{'start': '11:00', 'end': '14:00'}]
        req = {'tasks': refreshed['tasks'], 'base_revision': initial['preferences_revision']}
        assert client.post('/api/control/preferences', json=req).status_code == 200
        stale = client.post('/api/control/preferences', json=req)
        assert stale.status_code == 400
        assert 'reload' in stale.json()['detail'].lower()
    finally:
        c.close()


def test_connected_preview_uses_home_forecast_and_new_day_is_available(tmp_path, monkeypatch):
    class Home:
        bindings = {t.id: {} for t in load_demo_tasks()}
        def read_forecast(self, now):
            day = load_forecast_day()
            day.frame['import_usd_per_kwh'] = .91
            day.frame.index = day.frame.index + (now.date() - day.frame.index[0].date())
            return day
        def states(self, ids): return dict.fromkeys(ids, 'off')
        def set_state(self, tid, enabled): return 'on' if enabled else 'off'
    c = Controller(tmp_path / 'home.json', Home())
    fixed = datetime(2026, 9, 18, 8, 0, tzinfo=ZoneInfo('America/Los_Angeles'))
    monkeypatch.setattr(c, 'now', lambda: fixed)
    monkeypatch.setattr(app.state, 'controller', c, raising=False)
    client = TestClient(app)
    try:
        jobs = load_demo_tasks()
        day, _, now = c.preview(jobs)
        assert day.frame.import_usd_per_kwh.iloc[0] == .91 and now == '08:00'
        result = client.post('/api/plan', json={'tasks': [t.as_dict() for t in jobs]}).json()
        expected = run_day_ahead(jobs, day=day, now='08:00').as_dict()
        assert result['totals'] == expected['totals']
        c.state.update(tasks=[t.as_dict() for t in jobs], date=(fixed-timedelta(days=1)).date().isoformat(),
                       active=False, runs={t.id: {'status': 'completed', 'start':'09:00','end':'11:00'} for t in jobs})
        assert c.snapshot()['can_start_new_day']
        state = c.start(jobs)
        assert state['date'] == fixed.date().isoformat()
        assert (tmp_path / 'home-previous.json').exists()
        c.pause()
        with pytest.raises(ValueError, match='Resume'):
            c.start(jobs)
    finally:
        c.close()


def test_tariff_greedy_respects_constraints_and_reports_small_advantage():
    from loadshift.baseline import tariff_greedy_placements
    from loadshift.comparison import compare_day
    from loadshift.tasks import forbidden_steps
    jobs = load_demo_tasks()
    places = tariff_greedy_placements(jobs, load_forecast_day().frame)
    for job in jobs:
        steps = places[job.id]
        assert not set(steps) & forbidden_steps(job, 48)
        assert len(steps) == job.duration_steps()
        assert steps == list(range(steps[0], steps[-1]+1))
    row = compare_day('2024-07-15')
    assert row['verdict_vs_tariff_greedy'] == 'tie'
    assert abs(row['loadshift_versus_tariff_greedy']['bill_usd']) < .05

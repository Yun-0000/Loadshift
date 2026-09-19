from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
import json

import pytest
from fastapi.testclient import TestClient
from loadshift.app import app
from loadshift.controller import Controller
from loadshift.devices import DeviceError, HomeAssistant
from loadshift.fixtures import load_demo_tasks, load_forecast_day


def test_automatic_day_forecast_change_and_confirmed_execution(tmp_path):
    c = Controller(tmp_path / 'home.json')
    try:
        first = c.start(load_demo_tasks())
        starts = {t['id']: t['planned_start'] for t in first['plan']['when_to_run']}
        c.weather('clouds')
        changed = c.tick(advance=True)
        assert any(t['planned_start'] != starts[t['id']] for t in changed['plan']['when_to_run'])
        owned = {}
        for _ in range(30):
            state = c.tick(advance=True)
            assert state['error'] is None
            for tid, run in state['runs'].items():
                if tid in owned:
                    assert owned[tid] == (run['start'], run['end'])
                owned[tid] = (run['start'], run['end'])
            if not state['active']:
                break
        assert len(owned) == 3
        assert all(r['status'] == 'completed' for r in state['runs'].values())
        assert set(state['devices'].values()) == {'off'}
        assert len([e for e in state['events'] if e['kind'] == 'started']) == 3
        assert len([e for e in state['events'] if e['kind'] == 'completed']) == 3
    finally:
        c.close()


def test_pause_restart_and_midnight_finish(tmp_path):
    path = tmp_path / 'home.json'
    task = replace(load_demo_tasks()[0], duration_hours=.5, original_start='23:30',
                   must_finish_by='24:00', inconvenient_windows=())
    c = Controller(path)
    c.start([task], now='23:30')
    assert c.snapshot()['devices'][task.id] == 'on'
    with pytest.raises(RuntimeError, match='Another'):
        Controller(path)
    c.pause()
    c.close()
    c = Controller(path)
    try:
        assert not c.snapshot()['active']
        state = c.tick(advance=True)
        assert state['clock'] == '24:00'
        assert state['devices'][task.id] == 'off'
        assert state['runs'][task.id]['status'] == 'completed'
    finally:
        c.close()


class FakeHome:
    bindings = {t.id: {} for t in load_demo_tasks()}
    def __init__(self):
        self.devices = dict.fromkeys(self.bindings, 'off')
        self.calls = []
        self.unavailable = None
        self.fail_start = False
    def states(self, ids):
        if self.unavailable in ids:
            raise DeviceError('Device offline')
        return {i: self.devices[i] for i in ids}
    def set_state(self, tid, enabled):
        self.calls.append((tid, enabled))
        if not self.fail_start:
            self.devices[tid] = 'on' if enabled else 'off'
        return self.devices[tid]
    def read_forecast(self, now):
        return load_forecast_day()


def test_unconfirmed_start_is_not_reported_running_and_retries_require_resume(tmp_path, monkeypatch):
    home = FakeHome()
    home.fail_start = True
    c = Controller(tmp_path / 'home.json', home)
    fixed = datetime.now(ZoneInfo('America/Los_Angeles')).replace(hour=23, minute=30, second=0)
    monkeypatch.setattr(c, 'now', lambda: fixed)
    task = replace(load_demo_tasks()[0], duration_hours=.5, original_start='23:30', must_finish_by='24:00', inconvenient_windows=())
    try:
        state = c.start([task])
        assert not state['active'] and state['error']
        assert not state['runs']
        assert not any(e['kind'] == 'started' for e in state['events'])
        c.tick()
        assert len(home.calls) == 1
        home.fail_start = False
        state = c.resume()
        assert state['runs'][task.id]['status'] == 'running'
    finally:
        c.close()


def test_unrelated_offline_device_does_not_block_owned_finish(tmp_path, monkeypatch):
    home = FakeHome()
    c = Controller(tmp_path / 'home.json', home)
    fixed = datetime.now(ZoneInfo('America/Los_Angeles')).replace(hour=12, minute=0)
    monkeypatch.setattr(c, 'now', lambda: fixed)
    c.state.update(tasks=[t.as_dict() for t in load_demo_tasks()], date=fixed.date().isoformat(), active=True,
                   runs={'dishwasher': {'start': '10:00', 'end': '12:00', 'status': 'running'}})
    home.devices['dishwasher'] = 'on'
    home.unavailable = 'ev'
    try:
        state = c.tick()
        assert ('dishwasher', False) in home.calls
        assert state['runs']['dishwasher']['status'] == 'completed'
        assert state['error'] and not state['active']
    finally:
        c.close()


def test_ha_transport_calls_service_then_reads_actual_state(monkeypatch):
    config = {'url': 'http://homeassistant.local:8123', 'devices': {'dishwasher': {'entity_id': 'switch.dishwasher'}},
              'forecast_entity': 'sensor.forecast', 'pv_entity': 'sensor.pv', 'base_load_entity': 'sensor.base'}
    ha = HomeAssistant(config, 'test-token')
    requests = []
    class Response:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(self.body).encode()
    def transport(req, timeout):
        requests.append(req)
        assert req.headers['Authorization'] == 'Bearer test-token'
        return Response([] if req.data else {'state': 'off'})
    monkeypatch.setattr('loadshift.devices.urlopen', transport)
    assert ha.set_state('dishwasher', True) == 'off'
    assert requests[0].full_url.endswith('/api/services/switch/turn_on')
    assert json.loads(requests[0].data) == {'entity_id': 'switch.dishwasher'}
    assert requests[1].full_url.endswith('/api/states/switch.dishwasher')


def test_control_api_and_cross_origin_rejection(tmp_path, monkeypatch):
    c = Controller(tmp_path / 'home.json')
    monkeypatch.setattr(app.state, 'controller', c, raising=False)
    try:
        client = TestClient(app)
        assert client.post('/api/control/start', json={}, headers={'Origin': 'https://unrelated.test'}).status_code == 403
        response = client.post('/api/control/start', json={})
        assert response.status_code == 200 and response.json()['active']
        assert client.post('/api/control/pause').json()['active'] is False
        assert client.get('/api/control').json()['mode'] == 'demo'
    finally:
        c.close()


def test_live_forecast_units_and_stale_input_rejection():
    config = {'url': 'http://localhost:8123', 'devices': {}, 'forecast_entity': 'sensor.forecast',
              'pv_entity': 'sensor.pv', 'base_load_entity': 'sensor.base'}
    ha = HomeAssistant(config, 'test')
    now = datetime(2026, 9, 13, 10, 0, tzinfo=ZoneInfo('America/Los_Angeles'))
    attrs = {'date': '2026-09-13', 'updated_at': now.isoformat(), 'pv_w': [2000]*48,
             'load_w': [1000]*48, 'import_usd_per_kwh': [.3]*48, 'export_usd_per_kwh': [.05]*48}
    bodies = {'states/sensor.forecast': {'attributes': attrs},
              'states/sensor.pv': {'state': '1.25', 'last_reported': now.isoformat(), 'attributes': {'unit_of_measurement': 'kW'}},
              'states/sensor.base': {'state': '950', 'last_updated': now.isoformat(), 'attributes': {'unit_of_measurement': 'W'}}}
    ha._request = lambda path: bodies[path]
    frame = ha.read_forecast(now).frame
    assert frame.iloc[20].pv_w == 1250 and frame.iloc[20].load_w == 950
    attrs['updated_at'] = '2026-09-13T05:00:00-07:00'
    with pytest.raises(DeviceError, match='stale'):
        ha.read_forecast(now)


def test_parallel_rolling_and_manual_plans_keep_separate_masks():
    from concurrent.futures import ThreadPoolExecutor
    import cvxpy as cp
    from loadshift.planner import run_day_ahead
    from loadshift.tasks import TimeWindow, forbidden_steps
    task = load_demo_tasks()[0]
    morning = replace(task, inconvenient_windows=(TimeWindow('12:00', '24:00'),))
    afternoon = replace(task, inconvenient_windows=(TimeWindow('00:00', '14:00'),))
    original_solve = cp.Problem.solve
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_day_ahead, [morning], rolling=True)
        second = pool.submit(run_day_ahead, [afternoon])
        for plan, job in [(first.result(), morning), (second.result(), afternoon)]:
            assert not set(plan.tasks[0].steps) & forbidden_steps(job, 48)
    assert cp.Problem.solve is original_solve

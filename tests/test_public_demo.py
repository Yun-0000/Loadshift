from fastapi.testclient import TestClient
from loadshift.app import app
from loadshift.public_demo import COOKIE, encode, decode
from loadshift.controller import Controller
import copy
import time


def test_public_sessions_are_isolated_and_signed(monkeypatch):
    monkeypatch.setenv('LOADSHIFT_PUBLIC_DEMO', '1')
    monkeypatch.setenv('LOADSHIFT_SESSION_SECRET', 'independent-demo-test-secret-32-characters')
    with TestClient(app, base_url='https://demo.example') as first, TestClient(app, base_url='https://demo.example') as second:
        assert first.get('/api/demo').status_code == 200
        assert first.post('/api/control/start', json={}).json()['active']
        assert second.get('/api/control').json()['tasks'] == []
        assert first.get('/api/control').json()['tasks']
        assert len(first.cookies.get(COOKIE)) < 3800
        assert second.post('/api/control/start', json={}, headers={'origin': 'https://evil.example'}).status_code == 403
        first.cookies.clear()
        first.cookies.set(COOKIE, 'bad.signature')
        assert first.get('/api/control').json()['tasks'] == []


def test_signed_state_roundtrip_and_tampering():
    state = Controller(None).snapshot()
    state['issued'] = time.time()
    token = encode(state, 'secret')
    assert decode(token, 'secret')['mode'] == 'demo'
    assert decode(token, 'wrong') is None
    assert decode(token + 'x', 'secret') is None
    assert decode('x' * 4000, 'secret') is None


def test_reset_restores_defaults_without_affecting_other_visitors(monkeypatch):
    monkeypatch.setenv('LOADSHIFT_PUBLIC_DEMO', '1')
    monkeypatch.setenv('LOADSHIFT_SESSION_SECRET', 'independent-demo-test-secret-32-characters')
    with TestClient(app, base_url='https://demo.example') as first, TestClient(app, base_url='https://demo.example') as second:
        initial = first.get('/api/demo').json()
        assert initial['public_demo']
        tasks = copy.deepcopy(initial['tasks'])
        tasks[0]['inconvenient_windows'] = [{'start': '11:00', 'end': '14:00'}]
        assert first.post('/api/control/start', json={'tasks': tasks}).status_code == 200
        assert second.post('/api/control/start', json={}).status_code == 200
        assert first.post('/api/control/weather', json={'profile': 'clouds'}).status_code == 200
        assert first.post('/api/demo/reset', headers={'origin': 'https://other.example'}).status_code == 403
        assert first.get('/api/demo').json()['home']['tasks']
        assert first.post('/api/demo/reset').json() == {'reset': True}
        restored = first.get('/api/demo').json()
        assert len(restored['tasks']) == 3
        assert restored['tasks'] == initial['tasks']
        assert restored['home']['clock'] == '09:00'
        assert restored['home']['profile'] == 'sunny'
        assert not restored['home']['active']
        assert restored['home']['runs'] == {}
        assert restored['home']['plan'] is None
        assert restored['home']['events'] == []
        assert second.get('/api/demo').json()['home']['tasks']
        assert first.post('/api/plan', json={'tasks': restored['tasks']}).status_code == 200


def test_reset_cannot_clear_a_local_home(monkeypatch):
    monkeypatch.delenv('LOADSHIFT_PUBLIC_DEMO', raising=False)
    with TestClient(app) as client:
        assert client.post('/api/demo/reset').status_code == 403

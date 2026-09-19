from fastapi.testclient import TestClient
from loadshift.app import app
from loadshift.public_demo import COOKIE, encode, decode
from loadshift.controller import Controller
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

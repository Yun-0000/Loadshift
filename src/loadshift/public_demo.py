"""Isolated, signed demo state for a serverless preview; never connects devices."""
import base64
import hashlib
import hmac
import json
import time
import zlib

from loadshift.controller import Controller

COOKIE = 'loadshift_demo'


def encode(state: dict, secret: str) -> str:
    value = dict(state, events=state['events'][-12:])
    if value.get('plan'):
        value['plan'] = {k: v for k, v in value['plan'].items() if k != 'fixture'}
    raw = zlib.compress(json.dumps(value, separators=(',', ':'), allow_nan=False).encode())
    data = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    signature = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
    cookie = data + '.' + signature
    if len(cookie) > 3800:
        raise ValueError('Demo state is full. Use fewer devices or the local app.')
    return cookie


def decode(cookie: str, secret: str) -> dict | None:
    if len(cookie) > 3800:
        return None
    try:
        data, signature = cookie.rsplit('.', 1)
        expected = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        value = json.loads(zlib.decompress(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))))
        if value['mode'] != 'demo' or time.time() - value.get('issued', 0) > 86400:
            return None
        return value
    except (ValueError, KeyError, zlib.error):
        return None


def controller_for(cookie: str, secret: str) -> Controller:
    return Controller(None, initial=decode(cookie, secret))

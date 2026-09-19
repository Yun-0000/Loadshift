"""Home Assistant transport: service calls followed by independent state readback."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pandas as pd

from loadshift.fixtures import ForecastDay


class DeviceError(RuntimeError):
    pass


class HomeAssistant:
    def __init__(self, config: dict, token: str):
        self.config = config
        self.url = config['url'].rstrip('/')
        parsed = urlparse(self.url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Home Assistant URL must be http(s), without embedded credentials')
        if not token:
            raise ValueError('Set LOADSHIFT_HA_TOKEN before connecting Home Assistant')
        self.token = token
        self.bindings = config.get('devices', {})
        entities = [config.get('forecast_entity'), config.get('pv_entity'), config.get('base_load_entity')]
        for binding in self.bindings.values():
            entity = binding.get('entity_id', '')
            if not re.fullmatch(r'(switch|input_boolean)\.[a-z0-9_]+', entity):
                raise ValueError('Device mappings require switch or input_boolean entities')
            entities.append(entity)
        if len(set(b['entity_id'] for b in self.bindings.values())) != len(self.bindings):
            raise ValueError('Each device needs a distinct entity')
        if any(not isinstance(e, str) or not re.fullmatch(r'[a-z_]+\.[a-z0-9_]+', e) for e in entities):
            raise ValueError('Set forecast, PV and base-load entities in the connection file')

    def _request(self, path: str, payload: dict | None = None):
        request = Request(self.url + '/api/' + path,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=8) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise DeviceError('Home Assistant request failed' + (f' (HTTP {exc.code})' if isinstance(exc, HTTPError) else '')) from exc

    def _entity(self, entity: str) -> dict:
        value = self._request('states/' + entity)
        if not isinstance(value, dict) or not isinstance(value.get('attributes', {}), dict):
            raise DeviceError('Home Assistant returned an invalid entity')
        return value

    def states(self, task_ids: list[str]) -> dict[str, str]:
        result = {}
        for task_id in task_ids:
            if task_id not in self.bindings:
                raise DeviceError(f'No device mapping for {task_id}')
            state = self._entity(self.bindings[task_id]['entity_id']).get('state')
            if state not in {'on', 'off'}:
                raise DeviceError(f'{task_id} is unavailable')
            result[task_id] = state
        return result

    def set_state(self, task_id: str, enabled: bool) -> str:
        entity = self.bindings[task_id]['entity_id']
        domain = entity.split('.')[0]
        self._request(f'services/{domain}/turn_{"on" if enabled else "off"}', {'entity_id': entity})
        # A 200 response alone is not proof that the device accepted the command.
        return self.states([task_id])[task_id]

    def read_forecast(self, now: datetime) -> ForecastDay:
        forecast = self._entity(self.config['forecast_entity'])
        attrs = forecast.get('attributes', {})
        if attrs.get('date') != now.date().isoformat():
            raise DeviceError('Forecast must cover today in the home timezone')
        updated = datetime.fromisoformat(str(attrs.get('updated_at', '')).replace('Z', '+00:00'))
        if updated.tzinfo is None or not timedelta(minutes=-1) <= now - updated <= timedelta(hours=3):
            raise DeviceError('Forecast is stale; refresh it before starting devices')
        columns = {}
        for name in ['pv_w', 'load_w', 'import_usd_per_kwh', 'export_usd_per_kwh']:
            values = attrs.get(name)
            if not isinstance(values, list) or len(values) != 48:
                raise DeviceError(f'Forecast {name} must contain 48 half-hour values')
            if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in values):
                raise DeviceError('Forecast values must be numbers')
            values = [float(v) for v in values]
            if any(not math.isfinite(v) or (name.endswith('_w') and v < 0) for v in values):
                raise DeviceError('Forecast contains invalid values')
            columns[name] = values
        slot = now.hour * 2 + now.minute // 30
        for key, target in [('pv_entity', 'pv_w'), ('base_load_entity', 'load_w')]:
            reading = self._entity(self.config[key])
            reported = datetime.fromisoformat(str(reading.get('last_reported') or reading.get('last_updated', '')).replace('Z', '+00:00'))
            if reported.tzinfo is None or not timedelta(minutes=-1) <= now - reported <= timedelta(minutes=5):
                raise DeviceError('Power readings are stale')
            try:
                value = float(reading.get('state'))
            except (TypeError, ValueError) as exc:
                raise DeviceError('Power sensor is unavailable') from exc
            unit = reading.get('attributes', {}).get('unit_of_measurement')
            if unit == 'kW':
                value *= 1000
            elif unit != 'W':
                raise DeviceError('Power sensors must report W or kW')
            if not math.isfinite(value) or value < 0:
                raise DeviceError('Power sensor returned an invalid value')
            columns[target][slot] = value
        columns['ghi_wm2'] = [0.0] * 48
        index = pd.date_range(now.replace(hour=0, minute=0, second=0, microsecond=0), periods=48, freq='30min')
        if index[-1].date() != now.date() or index[-1].strftime('%H:%M') != '23:30':
            raise DeviceError('This controller requires a 48-slot local day')
        return ForecastDay(pd.DataFrame(columns, index=index), {'sources': {'connection': 'Home Assistant'}},
                           {'name': 'Connected home'}, {'source': 'Home Assistant forecasts and power readings'})

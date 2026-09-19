"""Single-home rolling scheduler and acknowledged device execution."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from loadshift.devices import DeviceError, HomeAssistant
from loadshift.fixtures import DEMO_DATE, ForecastDay, load_forecast_day
from loadshift.planner import run_day_ahead
from loadshift.tasks import Task, hhmm_to_step, parse_hhmm, tasks_from_payload


class Controller:
    def __init__(self, path: Path | None, adapter: HomeAssistant | None = None, timezone: str = 'America/Los_Angeles', initial: dict | None = None):
        self.path = path
        self._file_lock = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file_lock = path.with_suffix('.lock').open('a')
            try:
                fcntl.flock(self._file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('Another LoadShift controller owns this home; use one server worker') from exc
        self.lock = threading.RLock()
        self.adapter = adapter
        self.timezone = ZoneInfo(timezone)
        self.mode = 'home_assistant' if adapter else 'demo'
        self.state = copy.deepcopy(initial) if initial is not None else json.loads(path.read_text()) if path is not None and path.exists() else {
            'active': False, 'mode': self.mode, 'date': DEMO_DATE, 'clock': '09:00',
            'tasks': [], 'plan': None, 'runs': {}, 'devices': {}, 'events': [],
            'revision': 0, 'cycle_count': 0, 'error': None, 'profile': 'sunny', 'pending_commands': {},
        }
        if self.state['mode'] != self.mode and self.state['runs']:
            raise RuntimeError('Use a separate state file when switching homes')
        self.state['mode'] = self.mode
        self.state.setdefault('preferences_revision', 0)
        if initial is None:
            self.state['active'] = False  # Restart requires an explicit resume; no new starts.
        self.stop_event = threading.Event()
        self.thread = None
        self.save()

    def save(self):
        if self.path is None:
            return
        temp = self.path.with_suffix('.tmp')
        with temp.open('w') as output:
            json.dump(self.state, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        temp.replace(self.path)

    def event(self, kind: str, text: str, task_id: str | None = None):
        self.state['events'].append({'id': self.state.get('event_sequence', 0),
                                     'at': self.state['clock'], 'kind': kind, 'text': text, 'task_id': task_id})
        self.state['event_sequence'] = self.state.get('event_sequence', 0) + 1
        self.state['events'] = self.state['events'][-60:]

    def snapshot(self):
        with self.lock:
            value = copy.deepcopy(self.state)
            value['today'] = self.now().date().isoformat() if self.adapter else self.state['date']
            value['can_start_new_day'] = (not self.state['active']
                and not self.state['pending_commands']
                and not any(r['status'] == 'running' for r in self.state['runs'].values())
                and (not self.state['tasks'] or not self.adapter or value['today'] != self.state['date']))
            return value

    def preview(self, tasks: list[Task] | None = None, now: str | None = None):
        """Planning and execution read the same home and preserve owned jobs."""
        with self.lock:
            current = self.now()
            day = self._forecast(current)
            jobs = tasks if tasks is not None else self._tasks()
            if self.adapter and self.state['date'] != current.date().isoformat():
                jobs = [replace(t, status='pending', started_at=None) for t in jobs]
            if self.state['tasks'] and self.state['date'] == current.date().isoformat():
                owned = {t.id: t for t in self._tasks() if t.id in self.state['runs']}
                if any(tid not in {t.id for t in jobs} for tid in owned):
                    raise ValueError('Keep already-started jobs in the plan')
                jobs = [replace(t, status=owned[t.id].status, started_at=owned[t.id].started_at,
                                power_w=owned[t.id].power_w, duration_hours=owned[t.id].duration_hours)
                        if t.id in owned else t for t in jobs]
            if self.adapter or self.state['tasks']:
                now = current.strftime('%H:%M')
            return day, jobs, now

    def now(self):
        if self.adapter:
            return datetime.now(self.timezone)
        h, m = parse_hhmm(self.state['clock'])
        return datetime.fromisoformat(self.state['date']).replace(tzinfo=self.timezone) + timedelta(hours=h, minutes=m)

    def _read_states(self):
        ids = [t['id'] for t in self.state['tasks']]
        if self.adapter:
            self.state['devices'] = self.adapter.states(ids)
        else:
            self.state['devices'] = {i: self.state['devices'].get(i, 'off') for i in ids}

    def _command(self, task_id: str, enabled: bool, row: dict):
        desired = 'on' if enabled else 'off'
        # Persist intent before I/O. Unknown outcomes require readback before resuming.
        self.state['pending_commands'][task_id] = {'desired': desired, 'row': row}
        self.save()
        actual = self.adapter.set_state(task_id, enabled) if self.adapter else desired
        self.state['devices'][task_id] = actual
        if actual != desired:
            raise DeviceError(f'{task_id} did not confirm {desired}')
        self.state['pending_commands'].pop(task_id, None)
        self.event('started' if enabled else 'completed', f"{row['name']} {'started' if enabled else 'completed'} · confirmed {desired}", task_id)

    def _reconcile_intent(self):
        errors = []
        for tid, pending in list(self.state['pending_commands'].items()):
            desired, row = pending['desired'], pending['row']
            try:
                if self.adapter:
                    self.state['devices'].update(self.adapter.states([tid]))
                if self.state['devices'].get(tid) != desired:
                    raise DeviceError(f'{tid}: previous command is unconfirmed')
                if desired == 'on':
                    self.state['runs'][tid] = {'start': row['planned_start'], 'end': row['planned_end'], 'status': 'running'}
                elif tid in self.state['runs']:
                    self.state['runs'][tid]['status'] = 'completed'
                del self.state['pending_commands'][tid]
                self.event('reconciled', f"{row['name']} · recovered confirmed {desired}", tid)
            except (ValueError, RuntimeError, OSError) as exc:
                errors.append(str(exc))
        if errors:
            raise DeviceError('; '.join(errors))

    def _tasks(self):
        tasks = tasks_from_payload(self.state['tasks'])
        return [replace(t, status=self.state['runs'][t.id]['status'], started_at=self.state['runs'][t.id]['start'])
                if t.id in self.state['runs'] else t for t in tasks]

    def _stop_finished(self, now):
        errors = []
        for task in self._tasks():
            run = self.state['runs'].get(task.id)
            if not run or run['status'] != 'running':
                continue
            try:
                h, m = parse_hhmm(run['end'])
                end = datetime.fromisoformat(self.state['date']).replace(tzinfo=self.timezone) + timedelta(hours=h, minutes=m)
                if self.adapter:
                    self.state['devices'].update(self.adapter.states([task.id]))
                if now >= end:
                    if self.state['devices'][task.id] != 'off':
                        self._command(task.id, False, {'name': task.name, 'planned_start': run['start'], 'planned_end': run['end']})
                    else:
                        self.state['pending_commands'].pop(task.id, None)
                        self.event('completed', f'{task.name} completed · confirmed off', task.id)
                    run['status'] = 'completed'
                elif self.state['devices'][task.id] != 'on':
                    raise DeviceError(f'{task.name} stopped early; check the device before resuming')
            except (ValueError, RuntimeError, OSError) as exc:
                errors.append(str(exc))
        if errors:
            raise DeviceError('; '.join(errors))

    def start(self, tasks: list[Task], now: str = '09:00'):
        with self.lock:
            if self.state['active'] or any(r['status'] == 'running' for r in self.state['runs'].values()):
                raise ValueError('Pause or resume the current run before creating another')
            if self.state['pending_commands']:
                raise ValueError('Resolve the previous device command before creating another run')
            if any(t.status != 'pending' for t in tasks):
                raise ValueError('A new run needs pending jobs; resume an existing run to retain its progress')
            _, minute = parse_hhmm(now)
            if not self.adapter and minute % 30:
                raise ValueError('Use a half-hour start time, such as 09:00 or 09:30')
            if now == '24:00':
                raise ValueError('Start before the end of the day')
            if self.adapter and any(t.id not in self.adapter.bindings for t in tasks):
                raise ValueError('Map every device in the connection file first')
            if self.adapter and self.state['tasks'] and self.state['date'] == self.now().date().isoformat():
                raise ValueError('Resume this day; a new connected day starts on the next date')
            if self.state['tasks'] and self.path is not None:
                archive = self.path.parent / (self.path.stem + '-previous.json')
                archive.write_text(json.dumps(self.state, allow_nan=False))
            self.state.update(tasks=[t.as_dict() for t in tasks], runs={}, events=[], devices={},
                              date=self.now().date().isoformat() if self.adapter else DEMO_DATE,
                              clock=now, active=True, error=None, plan=None, revision=0, cycle_count=0, profile='sunny')
            self.state['preferences_revision'] += 1
            self.event('enabled', 'Automatic planning enabled')
            self.tick()
            return self.snapshot()

    def pause(self):
        with self.lock:
            self.state['active'] = False
            self.event('paused', 'New starts paused; running jobs keep their finish time')
            self.save()
            return self.snapshot()

    def resume(self):
        with self.lock:
            if not self.state['tasks']:
                raise ValueError('Create a plan first')
            if self.now().date().isoformat() != self.state['date']:
                raise ValueError('This run belongs to an earlier day')
            self._read_states()
            for tid, pending in list(self.state['pending_commands'].items()):
                if pending['desired'] == 'on' and self.state['devices'].get(tid) == 'off':
                    del self.state['pending_commands'][tid]
                    self.event('reconciled', 'Device is off; unconfirmed start cancelled', tid)
            self._reconcile_intent()
            self.state['active'] = True
            self.state['error'] = None
            self.event('resumed', 'Automatic planning resumed')
            self.tick()
            return self.snapshot()

    def preferences(self, tasks: list[Task], expected_revision: int | None = None):
        with self.lock:
            if expected_revision is not None and expected_revision != self.state['preferences_revision']:
                raise ValueError('Preferences changed in another window. Reload before applying edits.')
            incoming = {t.id: t for t in tasks}
            for existing in self._tasks():
                if existing.id in self.state['runs']:
                    t = incoming.get(existing.id)
                    if not t or (t.power_w, t.duration_hours) != (existing.power_w, existing.duration_hours):
                        raise ValueError('Keep already-started jobs and their duration/power unchanged')
            if self.adapter and any(t.id not in self.adapter.bindings for t in tasks):
                raise ValueError('Map new devices before updating the running controller')
            self.state['tasks'] = [replace(t, status='pending', started_at=None).as_dict() for t in tasks]
            self.state['preferences_revision'] += 1
            self.event('preferences', 'Updated preferences saved')
            self.tick()
            return self.snapshot()

    def weather(self, profile: str):
        with self.lock:
            if self.adapter:
                raise ValueError('Connected forecasts are read from Home Assistant')
            if profile not in {'sunny', 'clouds'}:
                raise ValueError('Choose sunny or clouds')
            self.state['profile'] = profile
            self.event('forecast', 'Solar forecast updated: ' + profile)
            self.save()
            return self.snapshot()

    def _forecast(self, now):
        if self.adapter:
            return self.adapter.read_forecast(now)
        day = load_forecast_day()
        frame = day.frame.copy()
        if self.state['profile'] == 'clouds':
            # An incoming midday cloud forecast; afternoon generation is unchanged.
            first = max(hhmm_to_step(self.state['clock']), 20)
            frame.iloc[first:28, frame.columns.get_loc('pv_w')] *= 0.15
        return ForecastDay(frame, day.provenance, day.location, day.method)

    def tick(self, advance: bool = False):
        with self.lock:
            if not self.state['tasks']:
                return self.snapshot()
            if advance and not self.adapter and (self.state['active'] or any(r['status'] == 'running' for r in self.state['runs'].values())):
                later = self.now() + timedelta(minutes=30)
                self.state['clock'] = later.strftime('%H:%M') if later.date().isoformat() == self.state['date'] else '24:00'
            now = self.now()
            self.state['clock'] = '24:00' if not self.adapter and now.date().isoformat() != self.state['date'] else now.strftime('%H:%M')
            try:
                try:
                    self._reconcile_intent()
                except DeviceError:
                    pass  # Finish owned jobs even while another command is unresolved.
                self._stop_finished(now)
                self._reconcile_intent()
                self.state['tasks'] = [t.as_dict() for t in self._tasks()]
                if not self.state['active']:
                    self.save()
                    return self.snapshot()
                if all(t['status'] == 'completed' for t in self.state['tasks']):
                    self.state['active'] = False
                    self.event('finished', 'All jobs completed')
                    self.save()
                    return self.snapshot()
                if now.date().isoformat() != self.state['date']:
                    raise DeviceError('Day finished; create the next day’s plan')
                self._read_states()
                for task in self._tasks():
                    if task.status == 'pending' and self.state['devices'][task.id] != 'off':
                        raise DeviceError(f'{task.name} is already on outside this run; check it before continuing')
                day = self._forecast(now)
                self.state['cycle_count'] += 1
                self.state['last_checked'] = now.isoformat()
                self.state['readings'] = {name: round(float(day.frame.iloc[hhmm_to_step(self.state['clock'])][name]), 1)
                                          for name in ['pv_w', 'load_w']}
                plan = run_day_ahead(self._tasks(), day=day, now=self.state['clock'], rolling=True).as_dict()
                before = {r['id']: r['planned_start'] for r in (self.state['plan'] or {}).get('when_to_run', [])}
                moved = [r['name'] for r in plan['when_to_run'] if r['id'] in before and before[r['id']] != r['planned_start']]
                self.state['plan'] = plan
                self.state['revision'] += 1
                if moved:
                    self.event('replanned', 'Updated schedule: ' + ', '.join(moved))
                elif self.state['revision'] == 1:
                    self.event('planned', 'Today’s plan is ready')
                for row in plan['when_to_run']:
                    if row['status'] == 'pending' and row['planned_start'] == self.state['clock']:
                        self._command(row['id'], True, row)
                        self.state['runs'][row['id']] = {'start': row['planned_start'], 'end': row['planned_end'], 'status': 'running'}
                        row['status'] = 'running'
                        self.save()
                self.state['tasks'] = [t.as_dict() for t in self._tasks()]
                if all(t['status'] == 'completed' for t in self.state['tasks']):
                    self.state['active'] = False
                    self.event('finished', 'All jobs completed')
                self.state['error'] = None
            except (ValueError, RuntimeError, KeyError, OSError) as exc:
                self.state['active'] = False
                if self.state['error'] != str(exc):
                    self.event('attention', str(exc))
                self.state['error'] = str(exc)
            self.save()
            return self.snapshot()

    def run_background(self, interval: float = 5):
        def work():
            while not self.stop_event.wait(interval):
                self.tick(advance=True)
        self.thread = threading.Thread(target=work, name='loadshift-controller', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        if self._file_lock:
            self._file_lock.close()


def configured_controller() -> Controller:
    config_path = os.environ.get('LOADSHIFT_HOME_CONFIG')
    config = json.loads(Path(config_path).read_text()) if config_path else {}
    adapter = HomeAssistant(config, os.environ.get('LOADSHIFT_HA_TOKEN', '')) if config else None
    return Controller(Path(os.environ.get('LOADSHIFT_STATE_PATH', '.loadshift/runtime.json')), adapter,
                      config.get('timezone', 'America/Los_Angeles'))

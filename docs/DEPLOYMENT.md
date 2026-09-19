# Public demo deployment

Production: https://loadshift-theta.vercel.app

Vercel project: `yun-fang-s-projects/loadshift`. Deploy from the project root with `vercel deploy --prod --scope yun-fang-s-projects`. The current setup uses CLI deployments; GitHub automatic deployment is not connected.

`vercel.json` targets the Python entrypoint in `api/index.py`; runtime dependencies are pinned in `requirements.txt`, with Python 3.12 selected. The deployment includes the app, UI and forecast fixtures, excluding video sources, local journals and credentials.

Set `LOADSHIFT_SESSION_SECRET` to a stable randomly generated value of at least 32 characters in the hosting environment. `LOADSHIFT_PUBLIC_DEMO=1` is already in the deployment configuration. Do not put the secret in git.

Each browser receives signed, compressed, HttpOnly, SameSite=Strict demo state, expiring after 24hours. The public mode has no device adapter or persistent home worker. It advances the accelerated home once per visible poll, so it can run on stateless workers. Separate browsers do not control each other's plans. Cookie size is bounded; use the local application for larger homes.

Local rehearsal:

```sh
uv sync --frozen
export LOADSHIFT_PUBLIC_DEMO=1
export LOADSHIFT_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
uv run uvicorn loadshift.app:app --port 8129
```

For actual Home Assistant devices, run one local server worker with the persistent journal, following HOME_ASSISTANT.md. Never expose that local control surface as the public demo.

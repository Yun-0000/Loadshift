# Connect a home

The default app includes an accelerated demo home: every five seconds advances the home clock by thirty minutes. Its background controller replans, starts jobs and confirms their completion. Choosing **Midday clouds** changes the incoming solar forecast; the next background cycle adjusts pending jobs.

For a connected home, use one local server worker. Copy `examples/home-assistant.json`, replace the entity IDs, and set:

```bash
export LOADSHIFT_HOME_CONFIG=/absolute/path/to/home-assistant.json
export LOADSHIFT_STATE_PATH=/absolute/path/to/private/home-state.json
read -s LOADSHIFT_HA_TOKEN
export LOADSHIFT_HA_TOKEN
uv run loadshift serve --host 127.0.0.1 --port 8000
```

Enter a Home Assistant long-lived access token at the hidden prompt. Keep it out of the JSON file and repository. Use a trusted local network or HTTPS. Open the local page and choose **Start automatic day**. Connected mode reads sensors and checks the schedule every 15 seconds. Clicking **Apply edited preferences** sends your edited deadlines/windows to the controller. The separate planner remains available for previewing changes before applying them.

Run the command from the project directory after `uv sync --frozen`.

## Entity contract

Each device maps to a distinct `switch` entity whose `turn_on` actually starts the intended job and whose `turn_off` stops it, or an `input_boolean` connected to a Home Assistant automation. A plain power socket does not necessarily start an appliance cycle: use a remote-start-capable device/automation and its appropriate state feedback. This adapter controls on/off jobs; continuous power setpoints are not exposed.

The adapter sends `POST /api/services/{domain}/turn_on` or `turn_off`, then independently reads `GET /api/states/{entity_id}`. Only a matching on/off state confirms the command. Home Assistant's [REST API documentation](https://developers.home-assistant.io/docs/api/rest/) describes these endpoints. Writing a state entity alone does not control hardware.

PV and base-load sensors must report nonnegative W or kW, with `last_reported` or `last_updated` within five minutes. Base load excludes the flexible jobs managed by LoadShift to avoid counting them twice.

Your forecast integration must expose one entity with these attributes:

| Attribute | Value |
| --- | --- |
| `date` | Today's `YYYY-MM-DD`, in the configured timezone |
| `updated_at` | ISO timestamp with offset, refreshed within three hours |
| `pv_w` | 48 half-hour mean solar power values, starting at local midnight |
| `load_w` | 48 half-hour base-load forecasts, excluding controlled jobs |
| `import_usd_per_kwh` | 48 import prices |
| `export_usd_per_kwh` | 48 export credits |

Map your solar/load/tariff integrations to this attribute schema. This is an explicit input contract, not an automatically discovered Home Assistant sensor. Current PV/load readings replace the current forecast slot before the rolling optimization. Days with a daylight-saving transition are rejected because the planner uses 48 slots.

## Execution and recovery

- Reoptimization preserves running/completed jobs and all user windows. Committed device power is included when planning remaining jobs.
- Pause blocks new starts; owned jobs retain their scheduled stop. Stops run before forecast reads, so an expired forecast cannot prevent a finish command.
- An offline device does not block attempts to finish other owned devices. Failed or unconfirmed commands pause new starts and remain in the durable journal for readback/recovery.
- A server restart keeps the run journal, checks owned jobs, and requires **Resume** before starting new jobs. Keep the server running through job completion; device-side timeout automation is appropriate if the host may lose power.
- This version schedules one local day at a time. Start the next day's plan explicitly.

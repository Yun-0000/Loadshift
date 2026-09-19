# Validation

Validation covers browser workflows, Home Assistant API integration and automated regressions.

- Browser acceptance covered 19 tasks, including sequential replanning, cloud-driven rescheduling, invalid inputs, navigation, reload recovery and mobile layouts. Failed cases were fixed and retested.
- Home Assistant 2026.9.3 acceptance used three virtual switches and authenticated HTTP calls to verify sensor units, authorization rejection, stale inputs, command readback and recovery.
- A scheduled execution check verified a wall-clock start, persistent restart recovery and stop/readback using an accelerated finish clock.
- [Scenario results](../artifacts/evaluation.json) retain four fixed scenarios × 14 days, including ties and strategy failures; regenerate them with the command below.

## Evaluation result

The default household's median modeled bill reduction against its original evening routine is $6.18/day. Against tariff-aware greedy, the median advantage is $0.00: 13 ties and one $0.08 win under the published tie rule. The greedy planner allows overlap and uses the same deadlines, excluded windows and 9kW grid-import limit. It tries all task orders for up to five pending tasks and three fixed orders above that.

Half solar, 1.5×solar and evening-only availability each retain all 14 dates. Their median bill savings against the original routine are $4.22, $6.33 and $1.13; median advantages against greedy remain zero. Solar-sequential is retained only as a secondary, stricter no-overlap heuristic. None of these estimates measures household bills or carbon reductions.

Reproduce:

```sh
uv sync --frozen --extra test
uv run pytest
npm test
uv run python scripts/evaluate_scenarios.py
```

CI runs the Python behavior regressions and frontend surface smoke check. The browser and Home Assistant acceptance checks were separate runs; they are not continuous tests in CI.

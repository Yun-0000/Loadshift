<p align="center">
  <img src="docs/images/loadshift-banner.svg" alt="LoadShift — Solar has a schedule. Life doesn't." width="100%">
</p>

<p align="center">
  Plan household energy around solar, electricity prices and everyday life.
</p>

<p align="center">
  <a href="https://loadshift-theta.vercel.app"><strong>Try LoadShift</strong></a> ·
  <a href="https://youtu.be/cglfkZVU4nE">Watch the demo · 3:04</a> ·
  <a href="#quick-start">Run locally</a> ·
  <a href="docs/HOME_ASSISTANT.md">Connect your home</a>
</p>

![LoadShift planning household jobs around solar and showing estimated energy savings](docs/images/todays-plan.png)

### A plan that fits your day

- **Plan.** Schedule the dishwasher, laundry and EV around solar, prices and your availability.
- **Adapt.** Change a time, keep earlier restrictions and see the cost before applying it.
- **Follow through.** Adjust pending jobs as forecasts change, confirm device states and let running jobs finish.

The [live app](https://loadshift-theta.vercel.app) needs no account or hardware. Generate a plan, change a time, then try **Auto run**. Each visitor has an independent sample home; reset it from **Devices**.

## Quick start

Requires **Python 3.12** and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
git clone https://github.com/Yun-0000/Loadshift.git
cd Loadshift
uv sync --frozen
uv run loadshift serve --port 8000
```

Open **http://127.0.0.1:8000**. Forecasts and three preset devices are included. Connect real devices with the [Home Assistant guide](docs/HOME_ASSISTANT.md).

## Built with

**Python · FastAPI · CVXPY · HiGHS · HTML / CSS / JavaScript · Home Assistant · Vercel**

## Documentation

[Home Assistant](docs/HOME_ASSISTANT.md) · [Development](docs/DEVELOPMENT.md) · [Deployment](docs/DEPLOYMENT.md) · [Tests & evaluation](docs/VALIDATION.md)

[Report an issue](https://github.com/Yun-0000/Loadshift/issues/new) · [MIT license](LICENSE)

<p align="center"><sub>Built for NextStep Hacks 2026 · Earth Forward</sub></p>

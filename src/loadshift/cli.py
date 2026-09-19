from __future__ import annotations

import argparse
import json

from loadshift.comparison import compare_days
from loadshift.fixtures import COMPARISON_DATES, load_demo_tasks, write_derived_csvs
from loadshift.planner import run_day_ahead
from loadshift.replan import replan
from loadshift.tasks import InfeasiblePlan


def _print_plan(plan) -> None:
    if plan.now:
        print(f"Current time: {plan.now} (past slots locked)")
    print("When to run")
    for task in plan.tasks:
        print(f"  - {task.when_to_run}")
    print("Versus original arrangement (estimates)")
    delta = plan.versus_original
    print(f"  bill:              {delta['bill_usd']:+.2f} USD")
    print(f"  grid-buy:          {delta['grid_buy_kwh']:+.2f} kWh")
    print(f"  self-consumption:  {delta['self_consumption_kwh']:+.2f} kWh")
    print(f"LoadShift status: {plan.solver_status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoadShift household coach")
    sub = parser.add_subparsers(dest="cmd", required=True)

    plan_p = sub.add_parser("plan", help="Run day-ahead on demo fixtures")
    plan_p.add_argument("--now", default=None, help="Current clock time HH:MM; past slots stay locked")

    replay = sub.add_parser("replan", help="Reschedule after a missed or inconvenient window")
    replay.add_argument("--task", required=True)
    replay.add_argument("--reason", default="doesnt_work", choices=["doesnt_work", "missed"])
    replay.add_argument("--from", dest="unavailable_start", required=True)
    replay.add_argument("--to", dest="unavailable_end", required=True)
    replay.add_argument("--now", default=None, help="Current clock time HH:MM")
    replay.add_argument(
        "--history",
        default=None,
        help="JSON list of prior {task_id,reason,unavailable_start,unavailable_end}",
    )

    serve = sub.add_parser("serve", help="Minimal web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    sub.add_parser("write-fixtures", help="Regenerate fixtures/day_ahead.csv")
    compare_p = sub.add_parser(
        "compare",
        help="Original vs tariff-aware greedy vs LoadShift on a pre-fixed consecutive window",
    )
    compare_p.add_argument(
        "--demo-days",
        action="store_true",
        help="Use the three demo-video days instead of the consecutive comparison window",
    )

    args = parser.parse_args(argv)
    if args.cmd == "write-fixtures":
        path = write_derived_csvs()
        print(path)
        return 0
    if args.cmd == "plan":
        try:
            plan = run_day_ahead(now=args.now)
        except InfeasiblePlan as exc:
            print(exc.payload["message"])
            for item in exc.relax:
                print(f"  relax: {item}")
            return 1
        _print_plan(plan)
        return 0
    if args.cmd == "replan":
        tasks = load_demo_tasks()
        history = json.loads(args.history) if args.history else None
        try:
            result = replan(
                tasks,
                args.task,
                args.reason,
                args.unavailable_start,
                args.unavailable_end,
                constraint_history=history,
                now=args.now,
            )
        except InfeasiblePlan as exc:
            print(exc.payload["message"])
            for item in exc.relax:
                print(f"  relax: {item}")
            return 1
        print(result["cost_of_change"]["headline"])
        print(f"Was: {result['previous_when']}")
        print(f"Now: {result['revised_when']}")
        if result.get("now"):
            print(f"Current time: {result['now']}")
        for change in result.get("changes") or []:
            print(f"  changed {change['name']}: {change['from']} → {change['to']} ({change['why']})")
        print(json.dumps(result["cost_of_change"]["versus_previous_plan"], indent=2))
        return 0
    if args.cmd == "compare":
        dates = COMPARISON_DATES if args.demo_days else None
        report = compare_days(dates=dates)
        labels = report["data_labels"]
        print(labels["weather"])
        print(labels["household_load"])
        print(labels["prices"])
        print(labels["carbon"])
        window = report["window"]
        print(
            f"Window {window['start']} → {window['end']} ({window['n_days']} consecutive days)"
        )
        print(
            f"{'date':<12} {'vs greedy':<6} {'orig $':>8} {'greedy $':>8} {'LS $':>8} "
            f"{'Δ$ vs greedy':>11} {'ΔkWh greedy':>10}"
        )
        for row in report["days"]:
            orig = row["strategies"]["original"]["totals"]
            seq = (row["strategies"].get("tariff_greedy") or {}).get("totals")
            load = (row["strategies"].get("loadshift") or {}).get("totals")
            versus = row.get("loadshift_versus_tariff_greedy") or {}
            verdict = row.get("verdict_vs_tariff_greedy") or "fail"
            seq_bill = seq["bill_usd"] if seq else float("nan")
            ls_bill = load["bill_usd"] if load else float("nan")
            print(
                f"{row['date']:<12} {verdict:<6} "
                f"{orig['bill_usd']:8.2f} {seq_bill:8.2f} {ls_bill:8.2f} "
                f"{versus.get('bill_usd', float('nan')):11.2f} "
                f"{versus.get('grid_buy_kwh', float('nan')):10.2f}"
            )
        dist = report["distribution"]["bill_usd_vs_tariff_greedy"]
        print(
            "Δbill vs tariff-aware greedy distribution: "
            f"min {dist.get('min')} median {dist.get('median')} "
            f"p90 {dist.get('p90')} max {dist.get('max')}"
        )
        if report["failures"]:
            print(f"Failures ({len(report['failures'])}):")
            for item in report["failures"]:
                print(f"  {item['date']} {item['who']}: {item['reason']}")
        summary = {**report["summary"], **report["summary"]["vs_tariff_greedy"]}
        print(
            f"vs tariff-aware greedy: win {summary['win'] or '—'}; "
            f"tie {summary['tie'] or '—'}; "
            f"lose {summary['lose'] or '—'}"
        )
        print(summary["analysis"])
        return 0
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("loadshift.app:app", host=args.host, port=args.port, reload=False)
        return 0
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
import textwrap
from pathlib import Path

from . import clock, compare, loader, policy, snapshot

PKG_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PKG_ROOT / "runs"
CURRENT_STATE = RUNS_DIR / "current" / "state.json"


def _next_run_dir(seed, days):
    base = RUNS_DIR / f"run-seed{seed}-{days}d"
    if not base.exists():
        return base
    n = 2
    while (RUNS_DIR / f"{base.name}-{n}").exists():
        n += 1
    return RUNS_DIR / f"{base.name}-{n}"


def cmd_load(args):
    folder = Path(args.folder)
    try:
        state, counts = loader.load_case(folder)
    except loader.LoadError as exc:
        print(f"load failed: {exc}")
        return 1
    snapshot.write_snapshot(state, CURRENT_STATE)
    print(f"loaded case {state.meta['case']!r} from {folder}")
    for name in sorted(counts):
        print(f"  {name:24} {counts[name]:5} rows")
    print(f"  {'total':24} {sum(counts.values()):5} rows")
    print(
        f"network: {len(state.nodes)} nodes, {len(state.lanes)} lanes, "
        f"{state.total_units()} units on hand, "
        f"${state.total_value():,.2f} stock value"
    )
    print(f"state written to {CURRENT_STATE}")
    return 0


def cmd_status(args):
    if not CURRENT_STATE.exists():
        print(f"no case loaded yet — run: python -m sim load <case folder>")
        return 1
    state = snapshot.read_snapshot(CURRENT_STATE)
    print(f"case: {state.meta['case']}    day: {state.day}")
    print(
        f"{'node':16} {'type':16} {'units':>7} {'cap':>7} "
        f"{'util%':>7} {'value $':>14}  flag"
    )
    for n in state.nodes:
        units = state.units_on_hand(n.code)
        value = state.value_on_hand(n.code)
        util = units / n.capacity_units * 100 if n.capacity_units else 0.0
        flag = "OVER CAP" if n.capacity_units and units > n.capacity_units else "ok"
        print(
            f"{n.code:16} {n.node_type:16} {units:7} {n.capacity_units:7} "
            f"{util:7.1f} {value:14,.2f}  {flag}"
        )
    print(
        f"total: {state.total_units()} units on hand, "
        f"${state.total_value():,.2f} stock value"
    )
    return 0


def cmd_run(args):
    if not CURRENT_STATE.exists():
        print(f"no case loaded yet — run: python -m sim load <case folder>")
        return 1
    state = snapshot.read_snapshot(CURRENT_STATE)
    state.meta["seed"] = args.seed
    out = Path(args.out) if args.out else _next_run_dir(args.seed, args.days)
    print(f"running {args.days} days (seed {args.seed}) -> {out}")
    for summary in clock.run_days(state, args.days, snapshot_dir=out):
        active = (
            summary["arrivals"] or summary["new_late"] or summary["new_orders"]
            or summary["transfers"] or summary["purchase_orders"] or summary["expedites"]
        )
        if active:
            print(
                f"  day {summary['day']:4}: {summary['new_orders']} orders, "
                f"{summary['transfers']} transfers, {summary['purchase_orders']} POs, "
                f"{summary['expedites']} expedites, {summary['arrivals']} arrivals, "
                f"{summary['new_late']} late"
            )
    filled = sum(1 for o in state.orders if o.status == "FULFILLED")
    pending = sum(1 for o in state.orders if o.status == "PENDING")
    late = sum(1 for o in state.orders if o.status == "LATE")
    units_ordered = sum(o.qty for o in state.orders)
    print(
        f"done: day {state.day}, {state.total_units()} units on hand, "
        f"{len(state.shipments)} shipments, {len(state.orders)} orders "
        f"({filled} filled / {pending} pending / {late} late, "
        f"{units_ordered} units ordered)"
    )
    return 0


def cmd_compare(args):
    if CURRENT_STATE.exists():
        current = snapshot.read_snapshot(CURRENT_STATE)
        folder = Path(current.meta["case_folder"])
    elif args.folder:
        folder = Path(args.folder)
    else:
        print("no loaded state — run: sim load <case-folder>")
        return 1
    if not folder.is_dir():
        print(f"case folder not found: {folder}")
        return 1
    out = compare.next_dir(RUNS_DIR, args.seed, args.days)
    print(
        f"comparing baseline vs optimized ({args.days} days, seed {args.seed})"
        f" -> {out}"
    )
    try:
        scorecard = compare.run_pair(
            folder, days=args.days, seed=args.seed,
            policy_name=args.policy, out_dir=out,
        )
    except (loader.LoadError, policy.PolicyError) as exc:
        print(f"compare failed: {exc}")
        return 1
    scenarios = scorecard["scenarios"]
    print(
        f"{'scenario':34} {'orders':>7} {'tier-1':>8} {'late u':>8} "
        f"{'holding/yr':>12} {'expedite/yr':>12} {'total/yr':>12}"
    )
    for name, label in kpis_labels():
        s = scenarios[name]
        svc = f"{s['tier1_service_pct']}%" if s['tier1_service_pct'] is not None else "n/a"
        print(
            f"{label:34} {s['orders']:7,} {svc:>8} {s['late_units']:8,} "
            f"{s['holding_annual']:12,.0f} {s['expedite_annual']:12,.0f} "
            f"{s['total_annual']:12,.0f}"
        )
    print()
    print(f"{'KPI':44} {'target':26} {'achieved':>16}  status")
    for row in scorecard["kpis"]:
        if row["unit"] == "$":
            achieved = f"${row['optimized']:,.0f}"
        elif row["unit"] == "$/yr":
            achieved = (
                f"${row['optimized']:,.0f}/yr"
                + (f" ({row['change_pct']:+.1f}%)"
                   if row.get("change_pct") is not None else "")
            )
        elif row["unit"] == "%":
            achieved = f"{row['optimized']}%"
        else:
            achieved = str(row["optimized"])
        print(
            f"{row['label']:44} {row['target']:26} {achieved:>16}  "
            f"{row['status'].upper()}"
        )
    print()
    print(f"overall: {scorecard['overall_status'].upper()}")
    print()
    print("verdict:")
    for paragraph in scorecard["verdict"].split("\n\n"):
        for line in textwrap.wrap(
                paragraph.replace("\u00a0", " "), width=96):
            print(f"  {line}")
        print()
    print(f"report written to {out / 'report.md'}")
    return 0


def kpis_labels():
    return (
        ("baseline", "baseline 1.00x"),
        ("optimized", "optimized 1.00x"),
        ("baseline_growth", "baseline growth"),
        ("optimized_growth", "optimized growth"),
    )


def cmd_serve(args):
    if not CURRENT_STATE.exists():
        print(f"no case loaded yet — run: python -m sim load <case folder>")
        return 1
    import uvicorn

    from .webapp import app
    print(f"serving network canvas at http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_apply_policy(args):
    if not CURRENT_STATE.exists():
        print(f"no case loaded yet — run: python -m sim load <case folder>")
        return 1
    state = snapshot.read_snapshot(CURRENT_STATE)
    try:
        result = policy.apply_policy(state, args.policy)
    except policy.PolicyError as exc:
        print(f"apply failed: {exc}")
        return 1
    snapshot.write_snapshot(state, CURRENT_STATE)
    t = result["totals"]
    print(f"applied MEIO-{args.policy} policy - replenishment engine enabled")
    print(f"  pairs changed: {result['changed_pairs']}")
    print(
        f"  network safety stock: {t['ss_before']} -> {t['ss_after']} units"
    )
    print(
        f"  network reorder points: {t['rop_before']} -> {t['rop_after']}"
    )
    for change in result["changes"][:8]:
        old = change["old"]
        new = change["new"]
        old_txt = "new pair" if old is None else (
            f"ROP {old['reorder_point']} / SS {old['safety_stock']}"
            f" / max {old['max_stock']}"
        )
        print(
            f"  {change['hub']}/{change['item']}: {old_txt} -> "
            f"ROP {new['reorder_point']} / SS {new['safety_stock']}"
            f" / max {new['max_stock']}"
        )
    if len(result["changes"]) > 8:
        print(f"  ... and {len(result['changes']) - 8} more")
    print(f"state written back to {CURRENT_STATE}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sim",
        description="Apex Hydraulics network-design simulator",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="load a case folder and initialize state")
    p_load.add_argument("folder")
    p_load.set_defaults(func=cmd_load)

    sub.add_parser("status", help="show the loaded network: units, caps, value")
    sub.choices["status"].set_defaults(func=cmd_status)

    p_run = sub.add_parser("run", help="advance the simulation N days")
    p_run.add_argument("--days", type=int, required=True)
    p_run.add_argument("--seed", type=int, default=42)
    p_run.add_argument("--out", default=None, help="snapshot output directory")
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="serve the network canvas web UI")
    p_serve.add_argument("--port", type=int, default=8710)
    p_serve.set_defaults(func=cmd_serve)

    p_apply = sub.add_parser(
        "apply-policy", help="write MEIO parameters into the loaded run and enable the engine",
    )
    p_apply.add_argument("--policy", choices=["pooled", "dispersed"], default="pooled")
    p_apply.set_defaults(func=cmd_apply_policy)

    p_cmp = sub.add_parser(
        "compare",
        help="run baseline vs optimized on one seed and score the case targets",
    )
    p_cmp.add_argument("--days", type=int, default=90)
    p_cmp.add_argument("--seed", type=int, default=42)
    p_cmp.add_argument("--policy", choices=["pooled", "dispersed"], default="pooled")
    p_cmp.add_argument("folder", nargs="?", default=None)
    p_cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)

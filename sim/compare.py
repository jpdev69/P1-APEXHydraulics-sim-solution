import json
from pathlib import Path

from . import clock, kpis, loader, policy, snapshot

PKG_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PKG_ROOT / "runs"

SCENARIO_SPECS = (
    ("baseline", 1.0, None),
    ("optimized", 1.0, "policy_name"),
    ("baseline_growth", "growth", None),
    ("optimized_growth", "growth", "policy_name"),
)
SCENARIO_FILES = {
    name: f"{name}-state.json" for name, _, _ in SCENARIO_SPECS
}


def next_dir(runs_root, seed, days):
    runs_root = Path(runs_root)
    base = runs_root / f"compare-seed{seed}-{days}d"
    if not base.exists():
        return base
    n = 2
    while (runs_root / f"{base.name}-{n}").exists():
        n += 1
    return runs_root / f"{base.name}-{n}"


def run_scenario(case_folder, days, seed, scale, apply=None):
    state, _ = loader.load_case(case_folder)
    state.meta["seed"] = seed
    state.meta["demand_scale"] = scale
    if apply:
        policy.apply_policy(state, apply)
    history = [state.total_value()]
    peaks = {n.code: state.units_on_hand(n.code) for n in state.nodes}
    for _ in clock.run_days(state, days):
        history.append(state.total_value())
        for n in state.nodes:
            peak = state.units_on_hand(n.code)
            if peak > peaks[n.code]:
                peaks[n.code] = peak
    return state, history, peaks


def run_pair(case_folder, days=90, seed=42, policy_name="pooled",
             capex_amounts=None, out_dir=None):
    peek, _ = loader.load_case(case_folder)
    tgts = kpis.targets(peek)
    growth_scale = round(1.0 + tgts["growth"], 4)
    parameters = {
        "days": days,
        "seed": seed,
        "policy": policy_name,
        "base_scale": 1.0,
        "growth_scale": growth_scale,
        "case": peek.meta["case"],
        "case_folder": peek.meta["case_folder"],
    }
    scenarios = {}
    states = {}
    for name, scale, applied in SCENARIO_SPECS:
        if scale == "growth":
            scale = growth_scale
        state, history, peaks = run_scenario(
            case_folder, days, seed, scale,
            apply=policy_name if applied else None,
        )
        states[name] = state
        scenarios[name] = kpis.run_kpis(state, history, days, peaks, tgts)
    amounts = capex_amounts or kpis.CAPEX_DEFAULTS
    capex = kpis.capex_ledger(amounts, tgts)
    rows, overall = kpis.score_kpis(scenarios, tgts, capex)
    scorecard = {
        "parameters": parameters,
        "targets": tgts,
        "scenarios": scenarios,
        "kpis": rows,
        "overall_status": overall,
        "capex": capex,
        "verdict": kpis.verdict_text(scenarios, tgts, capex, parameters),
    }
    if out_dir is not None:
        write_outputs(scorecard, states, Path(out_dir))
    return scorecard


def _report_markdown(scorecard, states):
    p = scorecard["parameters"]
    tg = scorecard["targets"]
    lines = [
        "# Apex Hydraulics - network redesign proof run",
        "",
        f"- case: **{p['case']}**",
        f"- run length: **{p['days']} days**, demand seed **{p['seed']}**",
        f"- scenarios: baseline (current network - each warehouse reorders "
        f"for itself on its own supplier lead times) vs optimized "
        f"(MEIO **{p['policy']}** policy, "
        f"{'plus the replenishment engine' if p['policy'] == 'pooled' else 'engine off - each warehouse reorders for itself'})",
        f"- demand levels: base **{p['base_scale']}x**, "
        f"growth **{p['growth_scale']}x** "
        f"(target +{round(tg['growth'] * 100)}% growth)",
        f"- targets: tier-1 service >= {round(tg['tier1_service'] * 100, 1)}%, "
        f"holding + expedite cost -15%, growth absorbed, "
        f"5,000-unit node caps never breached, capex <= "
        f"${tg['capex_ceiling']:,.0f}",
        "",
        f"Overall: **{scorecard['overall_status'].upper()}**",
        "",
        "## KPI scorecard",
        "",
        "| KPI | target | baseline | optimized | status |",
        "|---|---|---|---|---|",
    ]
    for row in scorecard["kpis"]:
        base_v = row.get("baseline")
        opt_v = row.get("optimized")
        if row["unit"] == "$":
            fmt = "${:,.0f}"
        elif row["unit"] == "$/yr":
            fmt = "${:,.0f}/yr"
        elif row["unit"] == "%":
            fmt = "{}%"
        else:
            fmt = "{}"
        base_txt = fmt.format(base_v) if base_v is not None else "-"
        opt_txt = fmt.format(opt_v) if opt_v is not None else "-"
        if row["key"] == "cost" and row.get("change_pct") is not None:
            opt_txt += f" ({row['change_pct']:+.1f}%)"
        lines.append(
            f"| {row['label']} | {row['target']} | {base_txt} "
            f"| {opt_txt} | {row['status'].upper()} |"
        )
    lines += ["", "## Scenario comparison", ""]
    for name, label in kpis.SCENARIO_LABELS:
        s = scorecard["scenarios"][name]
        lines += [
            f"### {label}",
            "",
            f"- orders: **{s['orders']:,}** "
            f"({s['filled']:,} filled / {s['pending']:,} pending at end / "
            f"{s['late']:,} late)",
            f"- tier-1 service: **{s['tier1_service_pct']}%** "
            f"({s['tier1_filled']:,} of {s['tier1_orders']:,})",
            f"- units shipped late or lost: **{s['late_units']:,}**",
            f"- avg inventory value: **${s['avg_inventory_value']:,.0f}** "
            f"-> holding **${s['holding_annual']:,.0f}/yr**",
            f"- expedite freight: **${s['expedite_annual']:,.0f}/yr** "
            f"(late-units premium "
            f"${s['late_freight']:,.2f} + engine expedites "
            f"{s['expedited_transfer_units']:,} units "
            f"${s['expedite_freight']:,.2f}, annualized)",
            f"- total holding + expedite: **${s['total_annual']:,.0f}/yr**",
            "",
        ]
    lines += ["## Capacity (peak units on hand, growth run)", ""]
    opt_g = scorecard["scenarios"]["optimized_growth"]
    lines += [
        "| node | type | capacity | peak | utilization | status |",
        "|---|---|---|---|---|---|",
    ]
    for code, c in opt_g["capacity"].items():
        status = "OVER" if c["breached"] else "ok"
        util = f"{c['peak_utilization_pct']}%" if c["peak_utilization_pct"] is not None else "n/a"
        lines.append(
            f"| {code} | {c['node_type']} | {c['capacity_units']:,} "
            f"| {c['peak_units']:,} | {util} | {status} |"
        )
    lines += ["", "## Capex ledger", ""]
    lines += ["| item | amount |", "|---|---|"]
    for item in scorecard["capex"]["items"]:
        lines.append(f"| {item['label']} | ${item['amount']:,.0f} |")
    lines += [
        f"| **total** | **${scorecard['capex']['total']:,.0f}** "
        f"(ceiling ${scorecard['capex']['ceiling']:,.0f}) |",
        "",
        "## Verdict",
        "",
        scorecard["verdict"],
        "",
        "## Event log summary",
        "",
        "Counts of logged events per scenario (full day-by-day event logs "
        "live in each scenario's state JSON).",
        "",
        "| event type | baseline | optimized | baseline growth | optimized growth |",
        "|---|---|---|---|---|",
    ]
    all_types = set()
    for state in states.values():
        all_types.update(kpis.event_summary(state))
    for etype in sorted(all_types):
        row = [etype]
        for name, _, _ in SCENARIO_SPECS:
            row.append(str(kpis.event_summary(states[name]).get(etype, 0)))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return "\n".join(lines)


def write_outputs(scorecard, states, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scorecard.json").write_text(
        json.dumps(scorecard, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    for name, state in states.items():
        snapshot.write_snapshot(state, out_dir / SCENARIO_FILES[name])
    (out_dir / "report.md").write_text(
        _report_markdown(scorecard, states), encoding="utf-8"
    )
    return out_dir

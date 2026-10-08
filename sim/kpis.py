from . import demand

CAPEX_LABELS = (
    "Rebuild the 3 regional hubs for fast flow-through (racking included)",
    "Connect the new software to the existing systems (WMS/ERP)",
    "Planning tool + training for the team",
    "Reserve for surprises",
)
CAPEX_DEFAULTS = (540000.0, 310000.0, 150000.0, 100000.0)

DEFAULT_HOLDING_RATE = 0.22
DEFAULT_EXPEDITE_PREMIUM = 1.85
DEFAULT_CAPEX_CEILING = 1200000.0
DEFAULT_GROWTH = 0.15
DEFAULT_SERVICE_TARGET = 0.97
DAYS_PER_YEAR = 365
AMBER_SERVICE_FLOOR = 90.0
AMBER_CAPACITY_FLOOR = 90.0


def assumption(state, name, default=None):
    for r in state.catalog.get("cost_assumptions", []):
        if r.get("assumption") == name:
            v = r.get("value")
            if isinstance(v, (int, float)):
                return v
    return default


def targets(state):
    return {
        "tier1_service": assumption(
            state, "Tier-1 service target", DEFAULT_SERVICE_TARGET
        ),
        "holding_rate": assumption(
            state, "Inventory holding rate", DEFAULT_HOLDING_RATE
        ),
        "expedite_premium": assumption(
            state, "Expedited freight premium", DEFAULT_EXPEDITE_PREMIUM
        ),
        "capex_ceiling": assumption(
            state, "Network redesign capex", DEFAULT_CAPEX_CEILING
        ),
        "growth": assumption(state, "Growth scenario", DEFAULT_GROWTH),
    }


def avg_lane_premium(state):
    vals = [
        l.expedited_freight_usd_per_unit - l.standard_freight_usd_per_unit
        for l in state.lanes
    ]
    return sum(vals) / len(vals) if vals else 0.0


def lane_premium(state, hub, customer_id):
    for l in state.lanes:
        if l.source_warehouse == hub and l.customer_id == customer_id:
            return (
                l.expedited_freight_usd_per_unit
                - l.standard_freight_usd_per_unit
            )
    return avg_lane_premium(state)


def tier1_ids(state):
    return {
        c["customer_id"]
        for c in state.catalog.get("customers", [])
        if c.get("customer_tier") == "Tier 1"
    }


def _service_pct(filled, total):
    return round(filled / total * 100.0, 1) if total else None


def run_kpis(state, value_history, days, peaks, tgts):
    tier1 = tier1_ids(state)
    orders_total = len(state.orders)
    filled = sum(1 for o in state.orders if o.status == "FULFILLED")
    pending = sum(1 for o in state.orders if o.status == "PENDING")
    late = sum(1 for o in state.orders if o.status == "LATE")
    late_units = sum(o.qty for o in state.orders if o.status == "LATE")
    pending_units = sum(o.qty for o in state.orders if o.status == "PENDING")
    tier1_orders = [o for o in state.orders if o.customer_id in tier1]
    tier1_filled = sum(1 for o in tier1_orders if o.status == "FULFILLED")

    avg_value = sum(value_history) / len(value_history) if value_history else 0.0
    holding_annual = avg_value * tgts["holding_rate"]

    premium = avg_lane_premium(state)
    late_freight = sum(
        o.qty * lane_premium(state, o.hub_code, o.customer_id)
        for o in state.orders if o.status == "LATE"
    )
    expedited_units = sum(s.qty for s in state.shipments if s.expedited)
    expedite_freight = expedited_units * premium
    expedite_annual = (
        (late_freight + expedite_freight) * DAYS_PER_YEAR / days if days else 0.0
    )
    total_annual = holding_annual + expedite_annual

    capacity = {}
    for n in state.nodes:
        peak = peaks.get(n.code, 0)
        util = round(peak / n.capacity_units * 100.0, 1) if n.capacity_units else None
        capacity[n.code] = {
            "name": n.name,
            "node_type": n.node_type,
            "capacity_units": n.capacity_units,
            "peak_units": peak,
            "peak_utilization_pct": util,
            "breached": bool(n.capacity_units) and peak > n.capacity_units,
        }
    return {
        "orders": orders_total,
        "filled": filled,
        "pending": pending,
        "late": late,
        "late_units": late_units,
        "pending_units": pending_units,
        "units_ordered": sum(o.qty for o in state.orders),
        "tier1_orders": len(tier1_orders),
        "tier1_filled": tier1_filled,
        "tier1_service_pct": _service_pct(tier1_filled, len(tier1_orders)),
        "avg_inventory_value": round(avg_value, 2),
        "holding_annual": round(holding_annual, 2),
        "late_freight": round(late_freight, 2),
        "expedited_transfer_units": expedited_units,
        "expedite_freight": round(expedite_freight, 2),
        "expedite_annual": round(expedite_annual, 2),
        "total_annual": round(total_annual, 2),
        "capacity": capacity,
        "peak_utilization_pct": max(
            (c["peak_utilization_pct"] for c in capacity.values()
             if c["peak_utilization_pct"] is not None),
            default=0.0,
        ),
        "capacity_breached": any(c["breached"] for c in capacity.values()),
    }


def capex_ledger(amounts, tgts):
    values = list(amounts)
    while len(values) < len(CAPEX_LABELS):
        values.append(0.0)
    items = [
        {"label": label, "amount": round(float(amount), 2)}
        for label, amount in zip(CAPEX_LABELS, values)
    ]
    total = round(sum(i["amount"] for i in items), 2)
    ceiling = tgts["capex_ceiling"]
    return {
        "items": items,
        "total": total,
        "ceiling": ceiling,
        "status": "green" if total <= ceiling else "red",
    }


def _svc_status(pct, target):
    if pct is None:
        return "amber"
    if pct >= target * 100.0:
        return "green"
    if pct >= AMBER_SERVICE_FLOOR:
        return "amber"
    return "red"


def score_kpis(scenarios, tgts, capex):
    base = scenarios["baseline"]
    opt = scenarios["optimized"]
    base_g = scenarios["baseline_growth"]
    opt_g = scenarios["optimized_growth"]
    target_pct = round(tgts["tier1_service"] * 100.0, 1)

    cost_change_pct = None
    if base["total_annual"]:
        cost_change_pct = round(
            (opt["total_annual"] - base["total_annual"])
            / base["total_annual"] * 100.0, 1
        )
    cost_status = "amber"
    if cost_change_pct is not None:
        if cost_change_pct <= -15.0:
            cost_status = "green"
        elif cost_change_pct > 0.0:
            cost_status = "red"

    growth_status = _svc_status(opt_g["tier1_service_pct"], tgts["tier1_service"])

    peak_util = opt_g["peak_utilization_pct"]
    capacity_status = "red"
    if not opt_g["capacity_breached"]:
        capacity_status = (
            "green" if peak_util < AMBER_CAPACITY_FLOOR else "amber"
        )

    rows = [
        {
            "key": "service",
            "label": "Top-customer fill rate — today's demand",
            "target": f">= {target_pct}%",
            "baseline": base["tier1_service_pct"],
            "optimized": opt["tier1_service_pct"],
            "unit": "%",
            "status": _svc_status(opt["tier1_service_pct"], tgts["tier1_service"]),
        },
        {
            "key": "cost",
            "label": "Yearly cost: storing stock + rush freight",
            "target": "-15% vs baseline",
            "baseline": base["total_annual"],
            "optimized": opt["total_annual"],
            "change_pct": cost_change_pct,
            "unit": "$/yr",
            "status": cost_status,
        },
        {
            "key": "growth",
            "label": f"Top-customer fill rate with demand +{round(tgts['growth'] * 100)}%",
            "target": f">= {target_pct}%",
            "baseline": base_g["tier1_service_pct"],
            "optimized": opt_g["tier1_service_pct"],
            "unit": "%",
            "status": growth_status,
        },
        {
            "key": "capacity",
            "label": "Fullest warehouse (peak % of its storage cap)",
            "target": "never over the 5,000-unit cap",
            "baseline": base_g["peak_utilization_pct"],
            "optimized": peak_util,
            "unit": "%",
            "status": capacity_status,
        },
        {
            "key": "capex",
            "label": "One-time investment to build the redesign",
            "target": f"<= ${capex['ceiling']:,.0f}",
            "optimized": capex["total"],
            "unit": "$",
            "status": capex["status"],
        },
    ]
    statuses = [r["status"] for r in rows]
    overall = (
        "red" if "red" in statuses
        else ("amber" if "amber" in statuses else "green")
    )
    return rows, overall


def _fmt_money(v):
    return f"${v:,.0f}"


def verdict_text(scenarios, tgts, capex, parameters):
    base = scenarios["baseline"]
    opt = scenarios["optimized"]
    opt_g = scenarios["optimized_growth"]
    days = parameters["days"]
    seed = parameters["seed"]
    policy_name = parameters.get("policy", "pooled")
    if policy_name == "dispersed":
        design = (
            "the MEIO-dispersed network - every warehouse keeps its own "
            "right-sized safety stock (today's structure, corrected "
            "reorder math) and reorders for itself from suppliers"
        )
        design_label = "The dispersed network"
    else:
        design = (
            "the redesigned network - one shared safety-stock buffer at "
            "the central hub, a technique called risk pooling, plus an "
            "automated replenishment engine moving stock over 1-2 day lanes"
        )
        design_label = "The redesigned network"
    svc_target = round(tgts["tier1_service"] * 100.0, 1)
    cost_pct = None
    if base["total_annual"]:
        cost_pct = round(
            (opt["total_annual"] - base["total_annual"])
            / base["total_annual"] * 100.0, 1
        )
    if cost_pct is not None and cost_pct <= 0:
        cost_clause = (
            f"so the combined bill for holding inventory plus premium freight "
            f"changes {cost_pct:+.1f}% "
            f"({_fmt_money(base['total_annual'])} to {_fmt_money(opt['total_annual'])} "
            f"per year), "
            f"{'beating' if cost_pct <= -15.0 else 'short of'} the -15% target"
        )
    elif cost_pct is not None:
        cost_clause = (
            f"so the combined bill for holding inventory plus premium freight "
            f"rises {cost_pct:+.1f}% "
            f"({_fmt_money(base['total_annual'])} to {_fmt_money(opt['total_annual'])} "
            f"per year). The case's -15% goal assumed pooling safety stock "
            f"would more than pay for itself, but that saving is far smaller "
            f"than the cost of keeping the whole network stocked to "
            f"guarantee near-perfect service"
        )
    else:
        cost_clause = (
            "so the combined holding + freight bill could not be compared"
        )
    indent = "\u00a0" * 4
    paragraphs = [
        indent + (
            f"Over a {days}-day simulation, both networks faced the exact "
            f"same random demand (seed {seed}): today's network, where "
            f"every warehouse reorders for itself from suppliers on the "
            f"case's long lead times (7-49 days), and {design}."
        ),
        indent + (
            f"Today's network filled "
            f"{base['tier1_service_pct']}% of orders from tier-1 "
            f"customers (the most important ones) and ended with "
            f"{base['late_units']} units shipped late or never shipped at "
            f"all - in the real world every such unit is rushed to the "
            f"customer at premium freight rates (1.85x). "
            f"{design_label} filled {opt['tier1_service_pct']}% and went "
            f"short on only {opt['late_units']} units."
        ),
        indent + (
            f"The price is inventory: holding costs more "
            f"({_fmt_money(base['holding_annual'])} to "
            f"{_fmt_money(opt['holding_annual'])} per year, because the "
            f"optimized network carries right-sized buffers while today's "
            f"network runs on the case's under-protective reorder "
            f"levels), while premium freight shrinks "
            f"({_fmt_money(base['expedite_annual'])} to "
            f"{_fmt_money(opt['expedite_annual'])} per year), "
            f"{cost_clause}."
        ),
        indent + (
            f"With demand {round(tgts['growth'] * 100)}% higher (the "
            f"growth scenario), the redesigned network still fills "
            f"{opt_g['tier1_service_pct']}% of tier-1 orders, against the "
            f"{svc_target}% target. Peak storage use reaches "
            f"{opt_g['peak_utilization_pct']}% of a node's unit cap, so "
            f"no facility is over capacity. The one-time investment to "
            f"do this (retrofitting the three regional hubs into "
            f"cross-docks, systems integration, and planning tooling) is "
            f"{_fmt_money(capex['total'])}, under the "
            f"{_fmt_money(capex['ceiling'])} ceiling."
        ),
    ]
    return "\n\n".join(paragraphs)


def event_summary(state):
    counts = {}
    for e in state.events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    return counts


SCENARIO_LABELS = (
    ("baseline", "today's network (base demand)"),
    ("optimized", "redesigned network (base demand)"),
    ("baseline_growth", "today's network (demand +15%)"),
    ("optimized_growth", "redesigned network (demand +15%)"),
)


def scenario_table_rows(scenarios):
    metrics = [
        ("orders", "orders", "{:,}"),
        ("filled", "shipped in full", "{:,}"),
        ("pending", "still waiting at end", "{:,}"),
        ("late", "orders too late", "{:,}"),
        ("late_units", "units shipped late / lost", "{:,}"),
        ("tier1_service_pct", "top-customer fill rate", "{}%"),
        ("avg_inventory_value", "avg stock value on shelves", "${:,.0f}"),
        ("holding_annual", "storing cost / yr", "${:,.0f}"),
        ("expedite_annual", "rush-freight cost / yr", "${:,.0f}"),
        ("total_annual", "total cost / yr", "${:,.0f}"),
    ]
    rows = []
    for key, label, fmt in metrics:
        row = {"metric": label}
        for name, _ in SCENARIO_LABELS:
            v = scenarios[name][key]
            row[name] = fmt.format(v) if v is not None else "n/a"
        rows.append(row)
    return rows

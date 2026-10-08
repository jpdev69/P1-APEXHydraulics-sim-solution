"""MEIO: multi-echelon inventory optimization for the Apex network.

In plain language:

- sigma (σ) measures how bumpy demand is for a SKU. Two independent
  sources of bumpiness are combined: day-to-day noise (the item's
  demand CV) and the seasonal swing (the item's seasonality index):
  k = sqrt(CV^2 + seasonality^2), so σ_month = monthly demand x k.
- safety stock (SS) protects against running out while a refill is on
  its way: SS = z x σ over the lead time. z comes from the service
  target (97% -> z = 1.88) via the standard normal distribution.
- reorder point (ROP) = expected demand during the lead time + SS.
- max stock = ROP + one month of cycle stock, then scaled down to fit
  the node's storage cap if needed (never below ROP).

Two policies, one API:

- dispersed (baseline, current Apex): every regional hub holds its own
  full safety stock, sized on its local demand and its own long
  replenishment lead time (the case's planning lead times).
- risk-pooled (optimized): the central hub holds ONE pooled safety
  stock per SKU, sized on the combined demand of all hubs
  (σ_pooled = k x sqrt(sum of hub demands^2) — three bumpy streams
  partly cancel, so the pooled buffer is smaller than three separate
  ones). Regional cross-docks keep only thin buffers, sized on the
  short hub-to-spoke transit (lane average) instead of the full
  supplier lead time.

Nothing here mutates the run state — optimize() is a pure function.
Applying a policy is the policy engine's job.
"""

import math
import statistics

from . import demand
from .metrics import tier1_target_pct

FALLBACK_SUPPLIER_LEAD_DAYS = 30
FALLBACK_SPOKE_LEAD_DAYS = 2
CAP_HEADROOM = 0.95


def _ceil(value):
    return max(0, math.ceil(value - 1e-9))


def supplier_lead_days(state, item_code):
    leads = [
        r["lead_time_days"]
        for r in state.catalog.get("item_suppliers", [])
        if r["item_code"] == item_code and isinstance(r.get("lead_time_days"), int)
    ]
    if leads:
        return min(leads)
    for r in state.catalog.get("items", []):
        if r["item_code"] == item_code:
            v = r.get("nominal_lead_time_days")
            if isinstance(v, int):
                return v
    return FALLBACK_SUPPLIER_LEAD_DAYS


def spoke_lead_days(state, hub_code):
    days = [
        lane.standard_transit_days
        for lane in state.lanes
        if lane.source_warehouse == hub_code and lane.standard_transit_days
    ]
    if days:
        return max(1, round(sum(days) / len(days)))
    return FALLBACK_SPOKE_LEAD_DAYS


def item_k(state, item_code):
    cv = demand.demand_cv(state.catalog, item_code)
    season = 0.0
    for r in state.catalog.get("items", []):
        if r["item_code"] == item_code:
            v = r.get("seasonality_index")
            if isinstance(v, (int, float)):
                season = v
            break
    return math.sqrt(cv * cv + season * season)


def _row(monthly, sigma_month, lead_days, z):
    sigma_dlt = sigma_month * math.sqrt(lead_days / 30.0)
    ss = _ceil(z * sigma_dlt)
    rop = _ceil(monthly * lead_days / 30.0 + ss)
    max_stock = _ceil(rop + monthly)
    return {
        "avg_monthly_demand": round(monthly, 2),
        "sigma_month": round(sigma_month, 2),
        "lead_time_days": lead_days,
        "sigma_dlt": round(sigma_dlt, 2),
        "safety_stock": ss,
        "reorder_point": rop,
        "max_stock": max_stock,
    }


def _cap_fit(state, hub_code, items):
    node = state.node(hub_code)
    capacity = node.capacity_units
    total_max = sum(r["max_stock"] for r in items.values())
    fitted = False
    for _ in range(3):
        if not capacity or total_max <= capacity:
            break
        share = capacity * CAP_HEADROOM / total_max
        for row in items.values():
            scaled = _ceil(row["max_stock"] * share)
            row["max_stock"] = max(row["reorder_point"], scaled)
        total_max = sum(r["max_stock"] for r in items.values())
        fitted = True
    return {
        "capacity_units": capacity,
        "total_safety_stock": sum(r["safety_stock"] for r in items.values()),
        "total_max_stock": total_max,
        "over_capacity": bool(capacity) and total_max > capacity,
        "capacity_fit": fitted,
        "items": items,
    }


def _dispersed(state, z):
    nodes = {}
    for hub in sorted(state.planning):
        items = {}
        for item in sorted(state.planning[hub]):
            params = state.planning[hub][item]
            monthly = params.avg_monthly_demand_units
            k = item_k(state, item)
            sigma_month = monthly * k
            items[item] = _row(monthly, sigma_month, params.lead_time_days, z)
        nodes[hub] = _cap_fit(state, hub, items)
    return {
        "nodes": nodes,
        "total_safety_stock": sum(
            b["total_safety_stock"] for b in nodes.values()
        ),
    }


def _pooled(state, z):
    central = demand.central_code(state)
    per_item = {}
    for hub, pairs in state.planning.items():
        for item, params in pairs.items():
            entry = per_item.setdefault(item, {"monthly_by_hub": {}})
            if params.avg_monthly_demand_units:
                entry["monthly_by_hub"][hub] = params.avg_monthly_demand_units
    nodes = {}
    central_items = {}
    for item in sorted(per_item):
        monthly_by_hub = per_item[item]["monthly_by_hub"]
        if not monthly_by_hub:
            continue
        k = item_k(state, item)
        pooled_monthly = sum(monthly_by_hub.values())
        sigma_pooled = k * math.sqrt(
            sum(m * m for m in monthly_by_hub.values())
        )
        lead = supplier_lead_days(state, item)
        central_items[item] = _row(pooled_monthly, sigma_pooled, lead, z)
    if central_items:
        nodes[central] = _cap_fit(state, central, central_items)
    for hub in sorted(state.planning):
        if hub == central:
            continue
        items = {}
        spoke_lead = spoke_lead_days(state, hub)
        for item in sorted(state.planning[hub]):
            params = state.planning[hub][item]
            monthly = params.avg_monthly_demand_units
            k = item_k(state, item)
            items[item] = _row(monthly, monthly * k, spoke_lead, z)
        if items:
            nodes[hub] = _cap_fit(state, hub, items)
    return {
        "nodes": nodes,
        "total_safety_stock": sum(
            b["total_safety_stock"] for b in nodes.values()
        ),
    }


def optimize(state):
    target = tier1_target_pct(state) / 100.0
    z = statistics.NormalDist().inv_cdf(target)
    dispersed = _dispersed(state, z)
    pooled = _pooled(state, z)
    disp_ss = dispersed["total_safety_stock"]
    pool_ss = pooled["total_safety_stock"]
    reduction = (
        round((disp_ss - pool_ss) / disp_ss * 100.0, 1) if disp_ss else 0.0
    )
    return {
        "service_level_pct": round(tier1_target_pct(state), 1),
        "z": round(z, 4),
        "dispersed": dispersed,
        "pooled": pooled,
        "savings": {
            "dispersed_total_safety_stock": disp_ss,
            "pooled_total_safety_stock": pool_ss,
            "reduction_pct": reduction,
        },
    }

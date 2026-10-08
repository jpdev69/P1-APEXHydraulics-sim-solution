import random
from collections import defaultdict
from pathlib import Path

from . import demand, loader, meio
from .state import PlanningParams, Shipment


class PolicyError(Exception):
    pass


def _policy_rng(seed, day):
    return random.Random(f"apex-sim|{seed}|policy|{day:06d}")


def _premium(state):
    for r in state.catalog.get("cost_assumptions", []):
        if r.get("assumption") == "Expedited freight premium":
            v = r.get("value")
            if isinstance(v, (int, float)):
                return v
    return 1.85


def _fastest_supplier(state, item_code):
    best = None
    for r in state.catalog.get("item_suppliers", []):
        if r["item_code"] != item_code:
            continue
        lead = r.get("lead_time_days")
        if not isinstance(lead, int):
            continue
        if best is None or lead < best[1]:
            best = (r["supplier_id"], lead)
    return best


def _supplier_on_time(state, supplier_id):
    for r in state.catalog.get("suppliers", []):
        if r["supplier_id"] == supplier_id:
            v = r.get("on_time_rate")
            if isinstance(v, (int, float)):
                return v
    return 0.95


def _on_hand(state, node, item):
    pos = state.inventory.get(node, {}).get(item)
    return pos.qty if pos else 0


def _incoming(state, node, item=None):
    total = 0
    for sh in state.shipments:
        if sh.arrived or sh.destination != node:
            continue
        if item is None or sh.item_code == item:
            total += sh.qty
    return total


def _open_inbound(state, node, item):
    for sh in state.shipments:
        if not sh.arrived and sh.destination == node and sh.item_code == item:
            return True
    return False


def _next_inbound_eta(state, node, item):
    best = None
    for sh in state.shipments:
        if sh.arrived or sh.destination != node or sh.item_code != item:
            continue
        if best is None or sh.eta_day < best:
            best = sh.eta_day
    return best


def reference_state(state):
    case_folder = state.meta.get("case_folder")
    if case_folder and Path(case_folder).is_dir():
        try:
            return loader.load_case(case_folder)[0]
        except loader.LoadError:
            return state
    return state


def apply_policy(state, policy="pooled"):
    if policy not in ("pooled", "dispersed"):
        raise PolicyError(f"unknown policy {policy!r}")
    reference = reference_state(state)
    m = meio.optimize(reference)
    blocks = m[policy]["nodes"]
    central = demand.central_code(state)
    original_avg = {
        hub: {item: p.avg_monthly_demand_units for item, p in pairs.items()}
        for hub, pairs in state.planning.items()
    }
    changed = []
    totals = {"ss_before": 0, "ss_after": 0, "rop_before": 0, "rop_after": 0}
    for hub in sorted(blocks):
        block = blocks[hub]
        node_plan = state.planning.setdefault(hub, {})
        for item in sorted(block["items"]):
            row = block["items"][item]
            old = node_plan.get(item)
            avg = original_avg.get(hub, {}).get(item)
            if avg is None:
                avg = 0.0 if (policy == "pooled" and hub == central) \
                    else row["avg_monthly_demand"]
            new = PlanningParams(
                avg_monthly_demand_units=avg,
                lead_time_days=row["lead_time_days"],
                safety_stock_units=row["safety_stock"],
                reorder_point_units=row["reorder_point"],
                max_stock_units=row["max_stock"],
                planning_method=f"MEIO-{policy}",
            )
            totals["ss_before"] += old.safety_stock_units if old else 0
            totals["rop_before"] += old.reorder_point_units if old else 0
            totals["ss_after"] += new.safety_stock_units
            totals["rop_after"] += new.reorder_point_units
            if old is None or (
                old.reorder_point_units != new.reorder_point_units
                or old.safety_stock_units != new.safety_stock_units
                or old.max_stock_units != new.max_stock_units
                or old.lead_time_days != new.lead_time_days
            ):
                changed.append({
                    "hub": hub,
                    "item": item,
                    "old": None if old is None else {
                        "safety_stock": old.safety_stock_units,
                        "reorder_point": old.reorder_point_units,
                        "max_stock": old.max_stock_units,
                        "lead_time_days": old.lead_time_days,
                    },
                    "new": {
                        "safety_stock": new.safety_stock_units,
                        "reorder_point": new.reorder_point_units,
                        "max_stock": new.max_stock_units,
                        "lead_time_days": new.lead_time_days,
                    },
                })
            node_plan[item] = new
    state.meta["policy_engine"] = policy == "pooled"
    state.meta["applied_policy"] = policy
    return {
        "policy": policy,
        "changed_pairs": len(changed),
        "changes": changed,
        "totals": totals,
        "engine": policy == "pooled",
    }


def disable_engine(state):
    reference = reference_state(state)
    restored = 0
    if reference is not state:
        state.planning.clear()
        for hub, pairs in reference.planning.items():
            node_plan = state.planning.setdefault(hub, {})
            for item, params in pairs.items():
                node_plan[item] = PlanningParams(
                    avg_monthly_demand_units=params.avg_monthly_demand_units,
                    lead_time_days=params.lead_time_days,
                    safety_stock_units=params.safety_stock_units,
                    reorder_point_units=params.reorder_point_units,
                    max_stock_units=params.max_stock_units,
                    planning_method=params.planning_method,
                )
                restored += 1
    state.meta["policy_engine"] = False
    state.meta["applied_policy"] = None
    return {
        "policy": None,
        "restored_pairs": restored,
        "engine": False,
    }


def _self_replenish(state):
    seed = state.meta.get("seed")
    rng = _policy_rng(0 if seed is None else seed, state.day)
    day = state.day
    counter = len(state.shipments)
    purchase_orders = 0
    for hub in sorted(state.planning):
        for item in sorted(state.planning[hub]):
            params = state.planning[hub][item]
            if not params.max_stock_units:
                continue
            if _open_inbound(state, hub, item):
                continue
            position = _on_hand(state, hub, item) + _incoming(state, hub, item)
            if position >= params.reorder_point_units:
                continue
            qty = params.max_stock_units - position
            supplier = _fastest_supplier(state, item)
            if supplier:
                supplier_id, supplier_lead = supplier
            else:
                supplier_id, supplier_lead = "SUP-UNKNOWN", 14
            lead = params.lead_time_days or supplier_lead
            late_days = 0
            if rng.random() > _supplier_on_time(state, supplier_id):
                late_days = max(1, round(lead * 0.2))
            eta = day + lead + late_days
            counter += 1
            shipment = Shipment(
                shipment_id=f"S-{counter:04d}", kind="PURCHASE",
                origin=f"SUP:{supplier_id}", destination=hub,
                item_code=item, qty=qty, created_day=day, eta_day=eta,
            )
            state.shipments.append(shipment)
            purchase_orders += 1
            state.events.append({
                "day": day, "type": "po_created",
                "shipment_id": shipment.shipment_id,
                "supplier_id": supplier_id, "item_code": item, "qty": qty,
                "lead_days": lead, "late_days": late_days, "eta_day": eta,
                "destination": hub,
            })
    return {"transfers": 0, "purchase_orders": purchase_orders,
            "expedites": 0}


def replenish(state):
    if not state.meta.get("policy_engine"):
        return _self_replenish(state)
    seed = state.meta.get("seed")
    rng = _policy_rng(0 if seed is None else seed, state.day)
    day = state.day
    central = demand.central_code(state)
    counter = len(state.shipments)
    transfers = 0
    purchase_orders = 0
    expedites = 0

    central_plan = state.planning.get(central, {})
    for item in sorted(central_plan):
        params = central_plan[item]
        if not params.max_stock_units:
            continue
        if _open_inbound(state, central, item):
            continue
        position = _on_hand(state, central, item) + _incoming(state, central, item)
        if position >= params.reorder_point_units:
            continue
        qty = params.max_stock_units - position
        supplier = _fastest_supplier(state, item)
        if supplier:
            supplier_id, lead = supplier
        else:
            supplier_id, lead = "SUP-UNKNOWN", params.lead_time_days or 14
        late_days = 0
        if rng.random() > _supplier_on_time(state, supplier_id):
            late_days = max(1, round(lead * 0.2))
        eta = day + lead + late_days
        counter += 1
        shipment = Shipment(
            shipment_id=f"S-{counter:04d}", kind="PURCHASE",
            origin=f"SUP:{supplier_id}", destination=central,
            item_code=item, qty=qty, created_day=day, eta_day=eta,
        )
        state.shipments.append(shipment)
        purchase_orders += 1
        state.events.append({
            "day": day, "type": "po_created", "shipment_id": shipment.shipment_id,
            "supplier_id": supplier_id, "item_code": item, "qty": qty,
            "lead_days": lead, "late_days": late_days, "eta_day": eta,
            "destination": central,
        })

    premium = _premium(state)
    pending_qty = defaultdict(int)
    for o in state.orders:
        if o.status == "PENDING":
            pending_qty[(o.hub_code, o.item_code)] += o.qty
    for hub, item in sorted(pending_qty):
        if hub == central or not hub:
            continue
        if _on_hand(state, hub, item) > 0:
            continue
        if _open_inbound(state, hub, item):
            continue
        available = _on_hand(state, central, item)
        if available <= 0:
            continue
        cap = state.node(hub).capacity_units
        qty = min(pending_qty[(hub, item)], available)
        if cap:
            headroom = cap - state.units_on_hand(hub) - _incoming(state, hub)
            qty = min(qty, max(0, headroom))
        if qty <= 0:
            continue
        demand.take_stock(state, central, item, qty)
        counter += 1
        shipment = Shipment(
            shipment_id=f"S-{counter:04d}", kind="TRANSFER",
            origin=central, destination=hub, item_code=item,
            qty=qty, created_day=day, eta_day=day + 1, expedited=True,
        )
        state.shipments.append(shipment)
        expedites += 1
        state.events.append({
            "day": day, "type": "expedite", "shipment_id": shipment.shipment_id,
            "origin": central, "destination": hub, "item_code": item,
            "qty": qty, "eta_day": day + 1, "premium": premium,
            "pending_qty": pending_qty[(hub, item)],
        })

    for hub in sorted(state.planning):
        if hub == central:
            continue
        cap = state.node(hub).capacity_units
        for item in sorted(state.planning[hub]):
            params = state.planning[hub][item]
            if not params.max_stock_units:
                continue
            if _open_inbound(state, hub, item):
                continue
            position = _on_hand(state, hub, item) + _incoming(state, hub, item)
            if position >= params.reorder_point_units:
                continue
            needed = params.max_stock_units - position
            available = _on_hand(state, central, item)
            if available <= 0:
                state.events.append({
                    "day": day, "type": "replenish_blocked",
                    "hub": hub, "item_code": item, "reason": "central_dry",
                    "position": position,
                    "reorder_point": params.reorder_point_units,
                    "inbound_eta_day": _next_inbound_eta(state, central, item),
                })
                continue
            qty = min(needed, available)
            if cap:
                headroom = cap - state.units_on_hand(hub) - _incoming(state, hub)
                qty = min(qty, max(0, headroom))
            if qty <= 0:
                state.events.append({
                    "day": day, "type": "replenish_blocked",
                    "hub": hub, "item_code": item,
                    "reason": "no_capacity_headroom",
                    "position": position,
                    "reorder_point": params.reorder_point_units,
                    "inbound_eta_day": None,
                })
                continue
            demand.take_stock(state, central, item, qty)
            lead = max(1, params.lead_time_days
                       or meio.spoke_lead_days(state, hub))
            counter += 1
            shipment = Shipment(
                shipment_id=f"S-{counter:04d}", kind="TRANSFER",
                origin=central, destination=hub, item_code=item,
                qty=qty, created_day=day, eta_day=day + lead,
            )
            state.shipments.append(shipment)
            transfers += 1
            state.events.append({
                "day": day, "type": "transfer_created",
                "shipment_id": shipment.shipment_id, "origin": central,
                "destination": hub, "item_code": item, "qty": qty,
                "eta_day": day + lead,
            })
    return {
        "transfers": transfers,
        "purchase_orders": purchase_orders,
        "expedites": expedites,
    }

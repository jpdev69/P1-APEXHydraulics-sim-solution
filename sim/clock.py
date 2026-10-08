from pathlib import Path

from . import demand, policy
from .snapshot import write_snapshot
from .state import InventoryPosition


def advance_one_day(state):
    state.meta["day"] += 1
    day = state.meta["day"]
    arrivals = 0
    for sh in state.shipments:
        if sh.arrived or sh.eta_day > day:
            continue
        sh.arrived = True
        positions = state.inventory.setdefault(sh.destination, {})
        pos = positions.get(sh.item_code)
        if pos is None:
            pos = InventoryPosition(qty=0, value_usd=0.0)
            positions[sh.item_code] = pos
        pos.qty += sh.qty
        pos.value_usd = round(
            pos.value_usd + sh.qty * demand.unit_cost(state.catalog, sh.item_code),
            4,
        )
        state.events.append({
            "day": day,
            "type": "arrival",
            "shipment_id": sh.shipment_id,
            "kind": sh.kind,
            "origin": sh.origin,
            "destination": sh.destination,
            "item_code": sh.item_code,
            "qty": sh.qty,
        })
        arrivals += 1
    new_orders = demand.generate_orders(state)
    demand.fill_pending(state)
    replenished = policy.replenish(state)
    new_late = 0
    for order in state.orders:
        if order.status != "PENDING" or order.created_day >= day:
            continue
        order.days_pending += 1
        if order.days_pending >= order.promised_window_days:
            order.status = "LATE"
            state.events.append({
                "day": day,
                "type": "order_late",
                "order_id": order.order_id,
                "customer_id": order.customer_id,
                "item_code": order.item_code,
                "qty": order.qty,
            })
            new_late += 1
    return {
        "day": day,
        "arrivals": arrivals,
        "new_orders": new_orders,
        "new_late": new_late,
        "transfers": replenished["transfers"],
        "purchase_orders": replenished["purchase_orders"],
        "expedites": replenished["expedites"],
    }


def run_days(state, days, snapshot_dir=None):
    if snapshot_dir is not None:
        snapshot_dir = Path(snapshot_dir)
        write_snapshot(state, snapshot_dir / f"state-day-{state.day:03d}.json")
    for _ in range(days):
        summary = advance_one_day(state)
        if snapshot_dir is not None:
            write_snapshot(state, snapshot_dir / f"state-day-{state.day:03d}.json")
        yield summary

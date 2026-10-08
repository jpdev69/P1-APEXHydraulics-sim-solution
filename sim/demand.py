import math
import random

from .state import CustomerOrder

MONTH_DAYS = 30
DEFAULT_SCALE = 1.15
DEFAULT_CV = 0.2
FALLBACK_WINDOW_DAYS = 2
SCALE_MIN = 0.5
SCALE_MAX = 1.5


def month_of_day(day):
    return (day // MONTH_DAYS) % 12 + 1


def day_rng(seed, day):
    return random.Random(f"apex-sim|{seed}|{day:06d}")


def central_code(state):
    for n in state.nodes:
        if "plant" in (n.node_type or "").lower():
            return n.code
    return state.nodes[0].code if state.nodes else None


def region_hub(state, region):
    for n in state.nodes:
        if "regional" in (n.node_type or "").lower() and n.region == region:
            return n.code
    for n in state.nodes:
        if n.region == region:
            return n.code
    return None


def customers_for_hub(state, hub_code):
    served = []
    for c in state.catalog.get("customers", []):
        if region_hub(state, c.get("region")) == hub_code:
            served.append(c)
    return sorted(served, key=lambda c: c["customer_id"])


def demand_cv(catalog, item_code):
    for r in catalog.get("items", []):
        if r["item_code"] == item_code:
            v = r.get("demand_cv")
            if isinstance(v, (int, float)) and v > 0:
                return v
    return DEFAULT_CV


def seasonal_factor(catalog, item_code, month):
    values = {}
    for r in catalog.get("seasonal_demand", []):
        if r["item_code"] == item_code:
            v = r.get("avg_daily_demand_units")
            if isinstance(v, (int, float)):
                values[r["month_number"]] = v
    if not values:
        return 1.0
    row = values.get(month)
    if row is None:
        return 1.0
    mean = sum(values.values()) / len(values)
    return row / mean if mean else 1.0


def transit_window(state, hub, customer_id):
    best = None
    for lane in state.lanes:
        if lane.source_warehouse == hub and lane.customer_id == customer_id:
            days = lane.standard_transit_days
            if days and (best is None or days < best):
                best = days
    return best if best else FALLBACK_WINDOW_DAYS


def _poisson(rng, lam):
    if lam <= 0:
        return 0
    if lam > 25:
        value = rng.gauss(lam, math.sqrt(lam))
        return int(value) if value > 0 else 0
    threshold = math.exp(-lam)
    k = 0
    p = 1.0
    while p > threshold:
        k += 1
        p *= rng.random()
    return k - 1


def unit_cost(catalog, item_code):
    for r in catalog.get("items", []):
        if r["item_code"] == item_code:
            v = r.get("unit_cost_usd")
            if isinstance(v, (int, float)) and v > 0:
                return v
    for r in catalog.get("item_suppliers", []):
        if r["item_code"] == item_code:
            v = r.get("supplier_unit_cost_usd")
            if isinstance(v, (int, float)) and v > 0:
                return v
    return 0.0


def take_stock(state, node_code, item_code, qty):
    pos = state.inventory.get(node_code, {}).get(item_code)
    if pos is None or pos.qty < qty:
        return False
    share = pos.value_usd * qty / pos.qty if pos.qty else 0.0
    pos.qty -= qty
    pos.value_usd = round(pos.value_usd - share, 4)
    return True


def try_fill(state, order):
    hub = order.hub_code or region_hub(state, order.region) or ""
    if hub and take_stock(state, hub, order.item_code, order.qty):
        order.status = "FULFILLED"
        state.events.append({
            "day": state.day,
            "type": "order_filled",
            "order_id": order.order_id,
            "filled_from": hub,
            "item_code": order.item_code,
            "qty": order.qty,
            "customer_id": order.customer_id,
        })
        return True
    if state.meta.get("allow_overflow"):
        central = central_code(state)
        if central and central != hub and take_stock(state, central, order.item_code, order.qty):
            order.status = "FULFILLED"
            state.events.append({
                "day": state.day,
                "type": "order_filled",
                "order_id": order.order_id,
                "filled_from": central,
                "item_code": order.item_code,
                "qty": order.qty,
                "customer_id": order.customer_id,
                "overflow": True,
            })
            return True
    return False


def generate_orders(state):
    if state.meta.get("seed") is None:
        return 0
    day = state.day
    rng = day_rng(state.meta["seed"], day)
    scale = state.meta.get("demand_scale", DEFAULT_SCALE)
    counter = len(state.orders)
    created = 0
    for hub in sorted(state.planning):
        customers = customers_for_hub(state, hub)
        if not customers:
            continue
        for item in sorted(state.planning[hub]):
            params = state.planning[hub][item]
            monthly = params.avg_monthly_demand_units
            if not monthly:
                continue
            lam = (
                monthly / MONTH_DAYS
                * seasonal_factor(state.catalog, item, month_of_day(day))
                * scale
            )
            cv = demand_cv(state.catalog, item)
            jitter = min(3.0, max(0.1, rng.gauss(1.0, cv)))
            units = _poisson(rng, lam * jitter)
            if units <= 0:
                continue
            customer = rng.choice(customers)
            counter += 1
            order = CustomerOrder(
                order_id=f"SO-{counter:04d}",
                customer_id=customer["customer_id"],
                item_code=item,
                qty=units,
                region=customer["region"],
                created_day=day,
                promised_window_days=transit_window(state, hub, customer["customer_id"]),
                hub_code=hub,
            )
            state.orders.append(order)
            created += 1
            try_fill(state, order)
    return created


def fill_pending(state):
    filled = 0
    for order in state.orders:
        if order.status == "PENDING":
            if try_fill(state, order):
                filled += 1
    return filled


def monthly_by_hub(state, scale):
    out = {}
    for hub, items in state.planning.items():
        out[hub] = round(
            sum(p.avg_monthly_demand_units for p in items.values()) * scale, 1
        )
    return out


def clamp_scale(value):
    return min(SCALE_MAX, max(SCALE_MIN, value))

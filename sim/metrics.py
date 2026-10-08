from . import demand

WEEK_DAYS = 7


def tier1_target_pct(state):
    for r in state.catalog.get("cost_assumptions", []):
        if r.get("assumption") == "Tier-1 service target":
            v = r.get("value")
            if isinstance(v, (int, float)) and v > 0:
                return v * 100 if v <= 1 else v
    return 97.0


def _seasonal_table(state, item_codes):
    return {
        item: [
            demand.seasonal_factor(state.catalog, item, month)
            for month in range(1, 13)
        ]
        for item in sorted(item_codes)
    }


def build(state, item_code=None, hub_code=None):
    day = state.day
    scale = state.meta.get("demand_scale", demand.DEFAULT_SCALE)
    tier1_customers = {
        c["customer_id"]
        for c in state.catalog.get("customers", [])
        if c.get("customer_tier") == "Tier 1"
    }
    item_codes = {
        item
        for items in state.planning.values()
        for item in items
    }
    seasonal = _seasonal_table(state, item_codes)
    pair_exists = bool(
        item_code and hub_code
        and hub_code in state.planning
        and item_code in state.planning[hub_code]
    )

    n = day
    forecast = [0.0] * (n + 1)
    selected_forecast = [0.0] * (n + 1)
    for hub, items in state.planning.items():
        for item, params in items.items():
            monthly = params.avg_monthly_demand_units
            if not monthly:
                continue
            base = monthly / demand.MONTH_DAYS
            for d in range(1, n + 1):
                month = demand.month_of_day(d)
                lam = base * seasonal[item][month - 1] * scale
                forecast[d] += lam
                if pair_exists and hub == hub_code and item == item_code:
                    selected_forecast[d] += lam

    actual = [0] * (n + 1)
    filled = [0] * (n + 1)
    pending = [0] * (n + 1)
    late = [0] * (n + 1)
    selected_actual = [0] * (n + 1)
    tier1_orders = [0] * (n + 1)
    tier1_filled = [0] * (n + 1)
    for o in state.orders:
        d = o.created_day
        if not 1 <= d <= n:
            continue
        actual[d] += o.qty
        if o.status == "FULFILLED":
            filled[d] += 1
        elif o.status == "PENDING":
            pending[d] += 1
        else:
            late[d] += 1
        if pair_exists and o.hub_code == hub_code and o.item_code == item_code:
            selected_actual[d] += o.qty
        if o.customer_id in tier1_customers:
            tier1_orders[d] += 1
            if o.status == "FULFILLED":
                tier1_filled[d] += 1

    weekly = []
    week = 1
    while WEEK_DAYS * (week - 1) < n:
        start = WEEK_DAYS * (week - 1) + 1
        end = min(WEEK_DAYS * week, n)
        f = sum(forecast[start:end + 1])
        a = sum(actual[start:end + 1])
        orders = sum(filled[i] + pending[i] + late[i] for i in range(start, end + 1))
        filled_week = sum(filled[i] for i in range(start, end + 1))
        t1o = sum(tier1_orders[i] for i in range(start, end + 1))
        t1f = sum(tier1_filled[i] for i in range(start, end + 1))
        error = abs(f - a) / f * 100 if f > 0 else None
        weekly.append({
            "week": week,
            "from_day": start,
            "to_day": end,
            "forecast": round(f, 1),
            "actual": a,
            "error_pct": round(error, 1) if error is not None else None,
            "orders": orders,
            "filled": filled_week,
            "fill_rate_pct": round(filled_week / orders * 100, 1) if orders else None,
            "tier1_orders": t1o,
            "tier1_filled": t1f,
            "tier1_fill_rate_pct": round(t1f / t1o * 100, 1) if t1o else None,
        })
        week += 1

    cum_forecast = sum(forecast[1:])
    cum_actual = sum(actual[1:])
    mape = (
        abs(cum_forecast - cum_actual) / cum_forecast * 100
        if cum_forecast > 0 else None
    )

    out = {
        "day": day,
        "scale": scale,
        "tier1_target_pct": round(tier1_target_pct(state), 1),
        "days": list(range(1, n + 1)),
        "forecast_units": [round(v, 2) for v in forecast[1:]],
        "actual_units": actual[1:],
        "filled": filled[1:],
        "pending": pending[1:],
        "late": late[1:],
        "weekly": weekly,
        "cumulative": {
            "forecast": round(cum_forecast, 1),
            "actual": cum_actual,
            "mape_pct": round(mape, 1) if mape is not None else None,
        },
        "hubs": sorted(
            hub for hub, pairs in state.planning.items()
            if any(p.avg_monthly_demand_units for p in pairs.values())
        ),
        "items": sorted(item_codes),
        "selected": None,
    }
    if pair_exists:
        out["selected"] = {
            "item": item_code,
            "hub": hub_code,
            "forecast": [round(v, 2) for v in selected_forecast[1:]],
            "actual": selected_actual[1:],
            "avg_monthly_demand": state.planning[hub_code][item_code].avg_monthly_demand_units,
        }
    return out

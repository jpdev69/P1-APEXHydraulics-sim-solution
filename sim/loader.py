import csv
from pathlib import Path

from .state import InventoryPosition, Lane, Node, PlanningParams, RunState

REQUIRED_COLUMNS = {
    "warehouses": {
        "warehouse_code", "warehouse_name", "region", "max_capacity_units",
        "warehouse_type",
    },
    "warehouse_constraints": {"warehouse_code", "capacity_units"},
    "items": {
        "item_code", "item_name", "item_group", "unit_cost_usd",
        "nominal_lead_time_days", "demand_cv", "seasonality_index",
    },
    "suppliers": {"supplier_id", "supplier_name", "nominal_lead_time_days", "on_time_rate"},
    "item_suppliers": {"item_code", "supplier_id", "supplier_unit_cost_usd", "lead_time_days"},
    "customers": {"customer_id", "customer_tier", "region", "target_service_level_pct"},
    "transport_lanes": {
        "source_warehouse", "customer_id", "standard_freight_usd_per_unit",
        "expedited_freight_usd_per_unit", "standard_transit_days",
        "expedited_transit_days",
    },
    "stock_levels": {"warehouse_code", "item_code", "actual_qty", "stock_value_usd"},
    "reorder_levels": {
        "warehouse_code", "item_code", "avg_monthly_demand_units",
        "lead_time_days", "calculated_safety_stock_units",
        "calculated_reorder_point_units", "recommended_max_stock_units",
    },
    "seasonal_demand": {
        "item_code", "month_number", "forecast_demand_units", "avg_daily_demand_units",
    },
    "cost_assumptions": {"assumption", "value"},
    "scenario_assumptions": {"type", "metric", "value"},
}

INT_COLUMNS = {
    "warehouses": {"max_capacity_units"},
    "warehouse_constraints": {"capacity_units"},
    "items": {"nominal_lead_time_days"},
    "suppliers": {"nominal_lead_time_days"},
    "item_suppliers": {"lead_time_days"},
    "customers": {"target_service_level_pct"},
    "transport_lanes": {"standard_transit_days", "expedited_transit_days"},
    "stock_levels": {"actual_qty"},
    "reorder_levels": {
        "lead_time_days", "calculated_safety_stock_units",
        "calculated_reorder_point_units", "recommended_max_stock_units",
    },
    "seasonal_demand": {"month_number", "forecast_demand_units"},
}

FLOAT_COLUMNS = {
    "items": {"unit_cost_usd", "demand_cv", "seasonality_index", "annual_growth_assumption"},
    "suppliers": {"on_time_rate"},
    "item_suppliers": {"supplier_unit_cost_usd"},
    "transport_lanes": {"standard_freight_usd_per_unit", "expedited_freight_usd_per_unit"},
    "stock_levels": {"stock_value_usd"},
    "reorder_levels": {"avg_monthly_demand_units"},
    "seasonal_demand": {"avg_daily_demand_units"},
    "cost_assumptions": {"value"},
}

CATALOG_TABLES = (
    "items", "customers", "suppliers", "item_suppliers", "seasonal_demand",
    "cost_assumptions", "scenario_assumptions", "warehouse_constraints",
)


class LoadError(Exception):
    pass


def _read_table(folder, name):
    path = folder / f"{name}.csv"
    if not path.is_file():
        raise LoadError(f"missing table file: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise LoadError(f"table {name!r} has no header row")
        missing = REQUIRED_COLUMNS[name] - set(reader.fieldnames)
        if missing:
            raise LoadError(
                f"table {name!r} missing required column(s): {', '.join(sorted(missing))}"
            )
        rows = []
        for lineno, raw in enumerate(reader, start=2):
            row = dict(raw)
            row["_row"] = lineno
            _coerce(name, row, lineno)
            rows.append(row)
    return rows


def _coerce(table, row, lineno):
    for col in INT_COLUMNS.get(table, ()):
        raw = row.get(col)
        if raw is None or raw == "":
            continue
        try:
            row[col] = int(float(raw))
        except (TypeError, ValueError):
            raise LoadError(
                f"{table}.csv row {lineno}: column {col!r} is not an integer: {raw!r}"
            )
    for col in FLOAT_COLUMNS.get(table, ()):
        raw = row.get(col)
        if raw is None or raw == "":
            continue
        try:
            row[col] = float(raw)
        except (TypeError, ValueError):
            raise LoadError(
                f"{table}.csv row {lineno}: column {col!r} is not a number: {raw!r}"
            )


def _validate_references(tables):
    warehouses = {r["warehouse_code"] for r in tables["warehouses"]}
    items = {r["item_code"] for r in tables["items"]}
    suppliers = {r["supplier_id"] for r in tables["suppliers"]}
    customers = {r["customer_id"] for r in tables["customers"]}

    def check(table, column, valid, label):
        for row in tables[table]:
            if row[column] not in valid:
                raise LoadError(
                    f"{table}.csv row {row['_row']}: "
                    f"unknown {label} {row[column]!r}"
                )

    check("stock_levels", "warehouse_code", warehouses, "warehouse")
    check("stock_levels", "item_code", items, "item")
    check("reorder_levels", "warehouse_code", warehouses, "warehouse")
    check("reorder_levels", "item_code", items, "item")
    check("item_suppliers", "item_code", items, "item")
    check("item_suppliers", "supplier_id", suppliers, "supplier")
    check("transport_lanes", "source_warehouse", warehouses, "warehouse")
    check("transport_lanes", "customer_id", customers, "customer")
    check("seasonal_demand", "item_code", items, "item")
    check("warehouse_constraints", "warehouse_code", warehouses, "warehouse")


def _build_state(folder, tables):
    meta = {
        "case": folder.name,
        "case_folder": str(folder.resolve()),
        "day": 0,
        "seed": None,
        "demand_scale": 1.15,
        "allow_overflow": False,
    }
    nodes = [
        Node(
            code=r["warehouse_code"],
            name=r["warehouse_name"],
            region=r["region"],
            node_type=r["warehouse_type"],
            capacity_units=r["max_capacity_units"],
        )
        for r in tables["warehouses"]
    ]
    lanes = [
        Lane(
            source_warehouse=r["source_warehouse"],
            customer_id=r["customer_id"],
            source_region=r.get("source_region") or "",
            customer_region=r.get("customer_region") or "",
            standard_freight_usd_per_unit=r["standard_freight_usd_per_unit"],
            expedited_freight_usd_per_unit=r["expedited_freight_usd_per_unit"],
            standard_transit_days=r["standard_transit_days"],
            expedited_transit_days=r["expedited_transit_days"],
        )
        for r in tables["transport_lanes"]
    ]
    inventory = {}
    for r in tables["stock_levels"]:
        wh = inventory.setdefault(r["warehouse_code"], {})
        wh[r["item_code"]] = InventoryPosition(
            qty=r["actual_qty"], value_usd=r["stock_value_usd"]
        )
    planning = {}
    for r in tables["reorder_levels"]:
        wh = planning.setdefault(r["warehouse_code"], {})
        wh[r["item_code"]] = PlanningParams(
            avg_monthly_demand_units=r["avg_monthly_demand_units"],
            lead_time_days=r["lead_time_days"],
            safety_stock_units=r["calculated_safety_stock_units"],
            reorder_point_units=r["calculated_reorder_point_units"],
            max_stock_units=r["recommended_max_stock_units"],
            planning_method=r.get("planning_method") or "",
        )
    catalog = {
        name: [
            {k: v for k, v in row.items() if k != "_row"}
            for row in tables[name]
        ]
        for name in CATALOG_TABLES
    }
    return RunState(
        meta=meta, nodes=nodes, lanes=lanes, catalog=catalog,
        inventory=inventory, planning=planning,
    )


def load_case(folder):
    folder = Path(folder)
    if not folder.is_dir():
        raise LoadError(f"case folder not found: {folder}")
    tables = {name: _read_table(folder, name) for name in REQUIRED_COLUMNS}
    _validate_references(tables)
    counts = {name: len(rows) for name, rows in tables.items()}
    return _build_state(folder, tables), counts

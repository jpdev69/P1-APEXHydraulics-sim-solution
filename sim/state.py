from dataclasses import asdict, dataclass

ORDER_STATUSES = ("FULFILLED", "PENDING", "LATE")
SHIPMENT_KINDS = ("TRANSFER", "PURCHASE")


@dataclass
class Node:
    code: str
    name: str
    region: str
    node_type: str
    capacity_units: int


@dataclass
class Lane:
    source_warehouse: str
    customer_id: str
    source_region: str
    customer_region: str
    standard_freight_usd_per_unit: float
    expedited_freight_usd_per_unit: float
    standard_transit_days: int
    expedited_transit_days: int


@dataclass
class InventoryPosition:
    qty: int
    value_usd: float


@dataclass
class PlanningParams:
    avg_monthly_demand_units: float
    lead_time_days: int
    safety_stock_units: int
    reorder_point_units: int
    max_stock_units: int
    planning_method: str


@dataclass
class Shipment:
    shipment_id: str
    kind: str
    origin: str
    destination: str
    item_code: str
    qty: int
    created_day: int
    eta_day: int
    arrived: bool = False
    expedited: bool = False


@dataclass
class CustomerOrder:
    order_id: str
    customer_id: str
    item_code: str
    qty: int
    region: str
    created_day: int
    promised_window_days: int
    status: str = "PENDING"
    days_pending: int = 0
    hub_code: str = ""


class RunState:
    def __init__(self, meta, nodes, lanes, catalog, inventory, planning,
                 shipments=None, orders=None, events=None):
        self.meta = meta
        self.nodes = nodes
        self.lanes = lanes
        self.catalog = catalog
        self.inventory = inventory
        self.planning = planning
        self.shipments = shipments if shipments is not None else []
        self.orders = orders if orders is not None else []
        self.events = events if events is not None else []

    @property
    def day(self):
        return self.meta["day"]

    def node(self, code):
        for n in self.nodes:
            if n.code == code:
                return n
        raise KeyError(f"unknown node {code!r}")

    def units_on_hand(self, node_code):
        return sum(p.qty for p in self.inventory.get(node_code, {}).values())

    def value_on_hand(self, node_code):
        return sum(p.value_usd for p in self.inventory.get(node_code, {}).values())

    def total_units(self):
        return sum(self.units_on_hand(n.code) for n in self.nodes)

    def total_value(self):
        return round(sum(self.value_on_hand(n.code) for n in self.nodes), 2)

    def to_dict(self):
        return {
            "meta": dict(self.meta),
            "nodes": [asdict(n) for n in self.nodes],
            "lanes": [asdict(l) for l in self.lanes],
            "catalog": self.catalog,
            "inventory": {
                w: {i: asdict(p) for i, p in items.items()}
                for w, items in self.inventory.items()
            },
            "planning": {
                w: {i: asdict(p) for i, p in items.items()}
                for w, items in self.planning.items()
            },
            "shipments": [asdict(s) for s in self.shipments],
            "orders": [asdict(o) for o in self.orders],
            "events": [dict(e) for e in self.events],
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            meta=dict(data["meta"]),
            nodes=[Node(**n) for n in data["nodes"]],
            lanes=[Lane(**l) for l in data["lanes"]],
            catalog=data["catalog"],
            inventory={
                w: {i: InventoryPosition(**p) for i, p in items.items()}
                for w, items in data["inventory"].items()
            },
            planning={
                w: {i: PlanningParams(**p) for i, p in items.items()}
                for w, items in data["planning"].items()
            },
            shipments=[Shipment(**s) for s in data["shipments"]],
            orders=[CustomerOrder(**o) for o in data["orders"]],
            events=[dict(e) for e in data["events"]],
        )

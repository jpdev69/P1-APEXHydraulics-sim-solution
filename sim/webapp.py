import json
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from . import clock, compare, demand, kpis, loader, snapshot
from . import metrics as metrics_mod
from . import meio
from .policy import PolicyError
from .policy import apply_policy as apply_policy_fn
from .policy import disable_engine as disable_engine_fn
from .policy import reference_state
from .state import Shipment

SIM_DIR = Path(__file__).resolve().parent
PKG_ROOT = SIM_DIR.parent
DEFAULT_STATE_SOURCE = PKG_ROOT / "runs" / "current" / "state.json"

app = FastAPI(title="Apex Hydraulics network sim")
app.mount("/static", StaticFiles(directory=str(SIM_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(SIM_DIR / "templates"))

_lock = threading.Lock()
_state = None
_state_source = DEFAULT_STATE_SOURCE
_last_compare = None


def set_state_source(path):
    global _state, _state_source
    _state_source = Path(path)
    _state = None


def _load_state():
    if not _state_source.exists():
        return None
    return snapshot.read_snapshot(_state_source)


def _ensure_state():
    global _state
    if _state is None:
        _state = _load_state()
    return _state


def _load_layout():
    path = SIM_DIR / "static" / "layout.json"
    return json.loads(path.read_text(encoding="utf-8"))


def node_health(state, code):
    inventory = state.inventory.get(code, {})
    planning = state.planning.get(code, {})
    for item_code, pos in inventory.items():
        params = planning.get(item_code)
        if params is not None and pos.qty < params.reorder_point_units:
            return "BELOW"
    return "OK"


def _lane_stats(state):
    stats = {}
    for lane in state.lanes:
        entry = stats.setdefault(
            lane.source_warehouse, {"customers": 0, "transit_days": []}
        )
        entry["customers"] += 1
        entry["transit_days"].append(lane.standard_transit_days)
    return {
        wh: {
            "customers": entry["customers"],
            "avg_transit_days": round(
                sum(entry["transit_days"]) / len(entry["transit_days"]), 1
            ),
        }
        for wh, entry in stats.items()
    }


def _central_node(state):
    for n in state.nodes:
        if "plant" in n.node_type.lower():
            return n
    return state.nodes[0]


def _no_case_response():
    return JSONResponse({"error": "no case loaded — run: python -m sim load <folder>"}, status_code=503)


def _supplier_lane_paths(spokes, central_pos):
    sx, sy = 170, 83
    cx, cy = central_pos["x"], central_pos["y"]
    lanes = []
    for s in spokes:
        hx, hy = s["x"], s["y"]
        mx, my = (sx + hx) / 2.0, (sy + hy) / 2.0
        dx, dy = hx - sx, hy - sy
        length = (dx * dx + dy * dy) ** 0.5 or 1.0
        px, py = -dy / length, dx / length
        if (mx - cx) * px + (my - cy) * py < 0:
            px, py = -px, -py
        qx = min(890, max(30, mx + px * 110))
        qy = min(570, max(30, my + py * 110))
        lanes.append({
            "hub": s["code"],
            "qx": round(qx), "qy": round(qy),
            "d": f"M {sx} {sy} Q {qx:.0f} {qy:.0f} {hx} {hy}",
        })
    return lanes


@app.get("/")
def index(request: Request):
    state = _ensure_state()
    if state is None:
        return templates.TemplateResponse(
            request, "canvas.html", {
                "case": None, "nodes": [], "spokes": [], "central": None,
                "supplier_count": 0, "items": [], "day": 0,
                "supplier_arrow": None, "policy_engine": False,
                "supplier_lanes": [], "central_has_planning": False,
            },
        )
    layout = _load_layout()
    default_pos = {"x": 460, "y": 300}
    central = _central_node(state)
    nodes = []
    for n in state.nodes:
        pos = layout["nodes"].get(n.code, default_pos)
        nodes.append({
            "code": n.code,
            "name": n.name,
            "node_type": n.node_type,
            "capacity_units": n.capacity_units,
            "x": pos["x"],
            "y": pos["y"],
            "health": node_health(state, n.code),
        })
    spokes = [n for n in nodes if n["code"] != central.code]
    supplier_arrow = {
        "x1": 170, "y1": 83,
        "x2": layout["nodes"].get(central.code, default_pos)["x"],
        "y2": layout["nodes"].get(central.code, default_pos)["y"],
    }
    return templates.TemplateResponse(
        request, "canvas.html", {
            "case": state.meta["case"],
            "day": state.day,
            "nodes": nodes,
            "spokes": spokes,
            "central": central.code,
            "central_pos": layout["nodes"].get(central.code, default_pos),
            "supplier_count": len(state.catalog.get("suppliers", [])),
            "items": state.catalog.get("items", []),
            "lane_stats": _lane_stats(state),
            "supplier_arrow": supplier_arrow,
            "policy_engine": bool(state.meta.get("policy_engine")),
            "central_has_planning": bool(state.planning.get(central.code)),
            "supplier_lanes": _supplier_lane_paths(
                spokes, layout["nodes"].get(central.code, default_pos)),
        },
    )


@app.get("/state")
def get_state():
    state = _ensure_state()
    if state is None:
        return _no_case_response()
    resp = state.to_dict()
    resp["meta"].setdefault("policy_engine", False)
    resp["meta"].setdefault("applied_policy", None)
    resp["node_health"] = {n.code: node_health(state, n.code) for n in state.nodes}
    scale = state.meta.get("demand_scale", demand.DEFAULT_SCALE)
    resp["demand"] = {
        "armed": state.meta.get("seed") is not None,
        "scale": scale,
        "monthly_by_hub": demand.monthly_by_hub(state, scale),
    }
    return JSONResponse(resp)


def _tick_locked(state, days):
    summaries = []
    for _ in range(days):
        summaries.append(clock.advance_one_day(state))
    return summaries


@app.get("/dashboard")
def dashboard(request: Request):
    state = _ensure_state()
    return templates.TemplateResponse(
        request, "dashboard.html", {
            "case": state.meta["case"] if state else None,
            "day": state.day if state else 0,
        },
    )


@app.get("/optimize")
def optimize_page(request: Request):
    state = _ensure_state()
    result = None
    applied = None
    engine_on = False
    if state is not None:
        result = meio.optimize(reference_state(state))
        applied = state.meta.get("applied_policy")
        engine_on = bool(state.meta.get("policy_engine"))
    return templates.TemplateResponse(
        request, "optimize.html", {
            "case": state.meta["case"] if state else None,
            "result": result,
            "applied_policy": applied,
            "engine_on": engine_on,
        },
    )


@app.get("/scorecard")
def scorecard_page(request: Request):
    state = _ensure_state()
    return templates.TemplateResponse(
        request, "scorecard.html", {
            "case": state.meta["case"] if state else None,
            "capex_labels": kpis.CAPEX_LABELS,
            "capex_defaults": kpis.CAPEX_DEFAULTS,
        },
    )


def _parse_capex(raw):
    if not raw:
        return list(kpis.CAPEX_DEFAULTS)
    try:
        amounts = [float(v) for v in raw.split(",")]
    except ValueError:
        return None
    if len(amounts) != len(kpis.CAPEX_DEFAULTS):
        return None
    return amounts


@app.post("/compare")
def compare_endpoint(
    days: int = 90, seed: int = 42, policy: str = "pooled",
    capex: str = None,
):
    global _last_compare
    state = _ensure_state()
    if state is None:
        return _no_case_response()
    if policy not in ("pooled", "dispersed"):
        return JSONResponse(
            {"error": "policy must be 'pooled' or 'dispersed'"}, status_code=400
        )
    amounts = _parse_capex(capex)
    if amounts is None:
        return JSONResponse(
            {
                "error": "capex must be "
                f"{len(kpis.CAPEX_DEFAULTS)} comma-separated amounts",
            },
            status_code=400,
        )
    case_folder = state.meta.get("case_folder")
    if not case_folder:
        return JSONResponse(
            {"error": "loaded state has no case_folder — reload the case"},
            status_code=400,
        )
    with _lock:
        out_dir = compare.next_dir(compare.RUNS_DIR, seed, max(1, days))
        try:
            scorecard = compare.run_pair(
                case_folder, days=max(1, days), seed=seed,
                policy_name=policy, capex_amounts=amounts, out_dir=out_dir,
            )
        except (loader.LoadError, PolicyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    scorecard["report_path"] = str(out_dir / "report.md")
    _last_compare = scorecard
    return JSONResponse(scorecard)


@app.post("/policy/apply")
def apply_policy_endpoint(policy: str = "pooled"):
    with _lock:
        state = _ensure_state()
        if state is None:
            return _no_case_response()
        try:
            result = apply_policy_fn(state, policy)
        except PolicyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(result)


@app.post("/policy/disable")
def disable_policy_endpoint():
    with _lock:
        state = _ensure_state()
        if state is None:
            return _no_case_response()
        result = disable_engine_fn(state)
        return JSONResponse(result)


@app.get("/meio")
def get_meio():
    state = _ensure_state()
    if state is None:
        return _no_case_response()
    return JSONResponse(meio.optimize(reference_state(state)))


@app.get("/metrics")
def get_metrics(item: str = None, hub: str = None):
    state = _ensure_state()
    if state is None:
        return _no_case_response()
    return JSONResponse(
        metrics_mod.build(state, item_code=item, hub_code=hub)
    )


@app.post("/tick")
def tick():
    with _lock:
        state = _ensure_state()
        if state is None:
            return _no_case_response()
        summary = clock.advance_one_day(state)
    return JSONResponse(summary)


@app.post("/run")
def run(days: int = 1):
    with _lock:
        state = _ensure_state()
        if state is None:
            return _no_case_response()
        summaries = _tick_locked(state, max(0, days))
        return JSONResponse({
            "day": state.day,
            "ran": len(summaries),
            "arrivals": sum(s["arrivals"] for s in summaries),
            "new_late": sum(s["new_late"] for s in summaries),
        })


@app.post("/reset")
def reset():
    global _state
    with _lock:
        _state = _load_state()
        if _state is None:
            return _no_case_response()
        return JSONResponse({"day": _state.day, "units": _state.total_units()})


@app.post("/demand/scale")
def set_demand_scale(value: float = 1.15):
    with _lock:
        state = _ensure_state()
        if state is None:
            return _no_case_response()
        clamped = demand.clamp_scale(value)
        state.meta["demand_scale"] = clamped
        if state.meta.get("seed") is None:
            state.meta["seed"] = 42
        return JSONResponse({
            "scale": clamped,
            "seed": state.meta["seed"],
            "armed": True,
            "monthly_by_hub": demand.monthly_by_hub(state, clamped),
        })


@app.post("/demand/overflow")
def set_overflow(enabled: bool = False):
    with _lock:
        state = _ensure_state()
        if state is None:
            return _no_case_response()
        state.meta["allow_overflow"] = bool(enabled)
        return JSONResponse({"allow_overflow": state.meta["allow_overflow"]})


@app.post("/demo/shipment")
def demo_shipment(
    origin: str, destination: str, item_code: str,
    qty: int = 10, days: int = 3,
):
    with _lock:
        state = _ensure_state()
        if state is None:
            return _no_case_response()
        codes = {n.code for n in state.nodes}
        if origin not in codes or destination not in codes or origin == destination:
            return JSONResponse(
                {"error": "origin and destination must be two different nodes"},
                status_code=400,
            )
        if qty <= 0 or days < 1:
            return JSONResponse(
                {"error": "qty must be positive and days at least 1"},
                status_code=400,
            )
        pos = state.inventory.get(origin, {}).get(item_code)
        if pos is None or pos.qty < qty:
            available = pos.qty if pos else 0
            hint = ""
            if available <= 0:
                hint = (
                    " — note: on a freshly loaded case the central plant "
                    "starts empty; its supplier stock arrives once the "
                    "policy engine's first purchase orders land (about two "
                    "simulated weeks in). Try shipping from a regional hub "
                    "or run the sim a few days first."
                )
            return JSONResponse(
                {
                    "error": (
                        f"{origin} has only {available} of {item_code} on hand "
                        f"(requested {qty}){hint}"
                    )
                },
                status_code=400,
            )
        share = pos.value_usd * qty / pos.qty if pos.qty else 0.0
        pos.qty -= qty
        pos.value_usd = round(pos.value_usd - share, 4)
        shipment = Shipment(
            shipment_id=f"S-{len(state.shipments) + 1:04d}",
            kind="TRANSFER",
            origin=origin,
            destination=destination,
            item_code=item_code,
            qty=qty,
            created_day=state.day,
            eta_day=state.day + days,
        )
        state.shipments.append(shipment)
        return JSONResponse({
            "shipment_id": shipment.shipment_id,
            "day": state.day,
            "eta_day": shipment.eta_day,
        })

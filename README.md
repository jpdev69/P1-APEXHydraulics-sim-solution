# APEXHydraulics-sim-solution

An interactive network-design simulation for the Apex Hydraulics case:
prove, by simulation, that a central-hub + cross-dock network with
MEIO-driven dynamic reorder policies hits the business targets
(97% tier-1 service, -15% holding + expedite freight cost, absorbs
15% growth, capex <= $1.2M).

## Result (90-day proof run, seed 42)

`sim compare --days 90 --seed 42` scores the business targets on
identical demand streams - current network vs MEIO-pooled redesign:

| KPI | target | optimized | status |
|---|---|---|---|
| tier-1 service (base demand) | >= 97% | 100.0% (baseline 98.9%) | green |
| tier-1 service at +15% growth | >= 97% | 99.9% (baseline 98.0%) | green |
| peak node utilization | never over 5,000 | 71.0% | green |
| one-time capex | <= $1.2M | $1.1M | green |
| holding + expedite cost / yr | -15% | +110.1% | red - honestly |

The redesign delivers the service, growth, capacity, and capex targets.
The -15% cost goal fails honestly and the scorecard explains why: today's
network reorders each warehouse for itself on the case's long supplier
lead times, so it runs lean and misses 86 units over 90 days; the
redesign carries about twice the stock to guarantee near-perfect
service. Full scorecard: `sim compare`, or the scorecard tab of the
web app.

## The five components

```
                        case CSVs (read-only)
                                   |
                                   v
        +--------------------------------------------------+
        |                SIM CORE (Python)                  |
        |   network state  +  daily tick / event loop       |
        +------+------------------+-------------------+-----+
               |                  |                   |
       +-------v------+   +-------v-------+   +-------v-------+
       | DEMAND       |   | MEIO          |   | FORECAST      |
       | engine       |   | engine        |   | dashboard     |
       | + slider     |   | (risk pool)   |   |               |
       +-------+------+   +-------+-------+   +---------------+
               |                  |
               v                  v
       +-------+------+   +-------+-------+
       | ORDER QUEUE  |   | POLICY        |
       | (fill/pending)|  | engine        |
       +--------------+   +---------------+
               |                  |
               +--------+---------+
                        v
        +--------------------------------------------------+
        | WEB UI: network canvas + demand slider           |
        |        + forecast dashboard + KPI scorecard      |
        +--------------------------------------------------+
```

1. **Network visualization** - WH-CENTRAL + 3 regional hubs, lanes,
   live stock and flows.
2. **Demand slider simulation** - demand scales up/down (0.5x-1.5x;
   1.15x = the growth target); customer orders arrive per tick and
   queue as PENDING when stock is short.
3. **Forecast dashboard** - seasonal forecast, actual vs forecast,
   fill-rate trend vs the 97% target.
4. **MEIO engine** - multi-echelon safety-stock optimization; risk
   pooling at the central hub.
5. **Policy engine** - writes MEIO outputs into reorder levels and
   executes replenishment (supplier POs to hub, transfers hub to
   spokes) inside the tick loop.

## Architecture principles

- **One sim state, one clock.** A single Python process owns the
  network state; everything else reads or mutates it through the core.
- **CSVs are read-only inputs.** The sim loads a case folder at
  startup; it never edits the dataset. Sim runs are reproducible via
  a seed.
- **Baseline vs optimized.** Every run is comparable: the same demand
  seed against (a) current reorder levels = baseline, (b) MEIO +
  policy engine = optimized. The targets are only provable as a diff.
- **Simple stack.** Python + FastAPI/Jinja2, SVG/JS canvas,
  SQLite-free (state in memory, snapshots to JSON). No containers,
  no build chain.

## Run it

```
pip install -e .
sim load <case-folder>            # load case CSVs into runs/current/state.json
sim serve --port 8710             # web app: canvas / dashboard / plan / scorecard
sim compare --days 90 --seed 42   # CLI proof run + report
```

The web app's canvas tab runs the sim day by day (Play / Step / Run 30
days), arms the demand engine with a slider, applies the pooled MEIO
policy, and streams replenishment events live.

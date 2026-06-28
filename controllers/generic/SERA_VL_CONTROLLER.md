# Sera VL Controller (V2) — Design & Status

A V2 controller that ports the V1 `sera_market_making` VL / triangular strategy
so it can be launched from the Hummingbot API / Condor `/new_bot`
("Upload Custom Config") path, which only accepts V2 controller configs.

This is now a **full-parity** port of the V1 strategy's tick pipeline (no longer a
scaffold).

- Controller: `controllers/generic/sera_vl.py`
- Example config: `conf/controllers/sera_vl_xsgd_myrt.yml`
- Tests: `test/hummingbot/connector/exchange/sera/test_sera_vl_controller.py`
- Ported from: `hummingbot/strategy/sera_market_making/` (`sera_market_making.pyx`)

## Why a custom controller was necessary

`/new_bot` requires a **V2 controller config** (a YAML with an `id` field). The
Sera strategy is **V1** (`strategy: sera_market_making`) and its VL/triangular
logic has no V2 equivalent. Generic V2 controllers (`pmm_simple`, etc.) cannot
reproduce VL. See the project's `SERA_TESTNET_BUILD_AND_TEST.md` discussion.

## How VL actually works (the key constraint)

- The **VL batch submission is in the connector**: `SeraExchange.batch_order_create()`
  (`hummingbot/connector/exchange/sera/sera_exchange.py:261`) groups orders by
  "spent asset" and submits any group of ≥2 to the VL endpoint
  (`_execute_vl_batch_order_create`). VL triggers purely from calling
  `batch_order_create()` with grouped orders — there is no enable flag on the
  connector.
- The **triangular mirroring is in the strategy**: `c_vl_order_price` and
  `c_vl_order_amount` (`sera_market_making.pyx:1445-1490`) decide which sibling
  markets to mirror and how to size them. These are ported to Python in
  `SeraVLController._vl_order_price` / `_vl_order_amount`.
- **V2 executors never call `batch_order_create`** — they place orders one at a
  time via `connector.buy()/sell()`. So the standard executor path produces NO
  VL batches.

Because of that last point, this controller reaches the live connector via
`self.market_data_provider.connectors[...]` and calls `batch_order_create()` /
`cancel()` directly (mirroring what V1 does with `market.batch_order_create(...)`),
then returns an empty `ExecutorAction` list.

## Architecture decisions

### Controller-owned placement + controller-level accounting (not a custom executor)

We deliberately do **not** wrap submission in a `SeraVLExecutor(ExecutorBase)`.
Sera VL groups are created **and cancelled atomically** by the connector
(`_place_cancel` → `_cancel_vl_batch`), which conflicts with the per-order
executor lifecycle: a `StopExecutorAction` cancelling one leg kills the whole
on-chain group, but the executor only receives one `OrderCancelled` event and
would treat the sibling legs as still live for up to the status-poll interval
(`UPDATE_ORDER_STATUS_MIN_INTERVAL = 10s`). It would also force editing the
framework's closed `ExecutorConfigBase.type` Literal and
`ExecutorOrchestrator._executor_mapping`.

Instead, fill/PnL accounting is done **in the controller** (mirroring V1, which
also accounts in-strategy via `c_did_fill_order` / `_filled_base_balance` /
`_filled_buys/sells_balance`). The controller polls `connector.in_flight_orders`
each tick, aggregates fills/volume/fees, and surfaces them via
`update_processed_data()`, `get_custom_info()` (published over MQTT with the
performance report) and `to_format_status()`. Trade-off: the standard `/bots`
executor-PnL panel won't auto-populate; the data is in the controller telemetry
instead. **No framework files are modified.**

### Reuse vs reimplement of V1 utilities

- **Reused unchanged:** `MovingPriceBand` and `inventory_skew_calculator` from
  `hummingbot/strategy/sera_market_making/`.
- **Ported (not reused):** the budget constraint is a direct port of V1's manual
  running-balance loop (faithful, self-contained for the empty-book oracle case).
- **Reimplemented natively:** hanging orders, as group-aware logic
  (`_hanging_process_tick`, `_is_potential_hanging_order`, `_mark_pair_filled`).
  The V1 `HangingOrdersTracker` assumes per-order cancellation with a per-order
  `OrderCancelled` event, which the connector's atomic VL group-cancel breaks.

## What the controller implements (full V1 tick pipeline)

- [x] Config class with the full V1 knob surface (mapped 1:1 from
      `conf_serapmm_sera.yml`); both split-order-levels and simple ladder modes,
      plus `order_override`.
- [x] `create_base_proposal` → price band (static) + moving price band →
      ping-pong → order optimization → add transaction costs → inventory skew →
      filled-base-balance → budget constraint → filter-out-takers.
- [x] Hanging-orders `process_tick` (group-aware), cancel-on-max-age, tolerance
      defer (`order_refresh_tolerance_pct`), cancel-below-min-spread.
- [x] `to_create_orders` gate honouring `should_wait_order_cancel_confirmation`
      (waits for cancel acks before re-placing instead of same-tick churn).
- [x] Triangular VL mirroring across `vl_order_markets` (faithful port of the
      V1 math) and direct VL batch submission through the connector.
- [x] Reconciliation + fill/PnL accounting against the connector order tracker;
      ping-pong counters, `filled_order_delay` timers, `filled_base_balance`.
- [x] `update_markets` subscribes the connector to every VL market.
- [x] Unit tests covering every pipeline stage, VL math, reconciliation, gates,
      and an end-to-end dry run.

## Known fidelity notes

- Fills are observed by **polling** `connector.in_flight_orders` each control
  loop (~1s) rather than V1's instant fill events, so ping-pong /
  `filled_order_delay` timing lags by up to one loop + the status-poll interval.
- `price_source` external_market / custom_api delegates and the inventory-cost
  price delegate are not ported (rarely used with Sera).

## How to test

1. Unit tests + lint (no network):
   ```bash
   $CONDA run -n hummingbot pytest test/hummingbot/connector/exchange/sera/test_sera_vl_controller.py
   $CONDA run -n hummingbot flake8 controllers/generic/sera_vl.py test/hummingbot/connector/exchange/sera/test_sera_vl_controller.py
   ```
2. Validate the config loads and the controller class is discoverable:
   ```bash
   $CONDA run -n hummingbot python -c "import controllers.generic.sera_vl as m; print(m.SeraVLController, m.SeraVLControllerConfig)"
   ```
3. Rebuild the image so the controller is included (`controllers/` is COPYed in —
   see `Dockerfile`):
   ```bash
   docker buildx build --platform linux/amd64 -t hummingbot/hummingbot:sera-amd64 -f Dockerfile --load .
   ```
4. In Condor: `/new_bot` → upload `conf/controllers/sera_vl_xsgd_myrt.yml` →
   select the `hummingbot/hummingbot:sera-amd64` image → start.
5. Watch logs for `Placed N Sera VL legs across M markets.` and confirm VL
   batches appear on Sera testnet.

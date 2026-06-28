"""
Sera VL (Virtual Liquidity) market-making controller — V2, full V1 parity.

Purpose
=======
Make the Sera VL / triangular market-making strategy runnable from the V2
controller path (Hummingbot API / Condor `/new_bot` "Upload Custom Config"),
which only accepts V2 controller configs.

This is a faithful port of the V1 `sera_market_making` strategy
(`hummingbot/strategy/sera_market_making/`). It reproduces the full V1 tick
pipeline so behaviour matches the headless V1 strategy:

    create_base_proposal
      -> price band (static) + moving price band
      -> ping-pong
      -> order optimization
      -> add transaction costs
      -> inventory skew
      -> filled-base-balance
      -> budget constraint
      -> filter out takers
    -> hanging-orders process_tick
    -> cancel on max age
    -> cancel (with order_refresh_tolerance_pct defer)
    -> cancel below min spread
    -> to_create_orders gate (should_wait_order_cancel_confirmation)
    -> execute proposal as a Sera VL batch

Architecture decision — why this bypasses V2 executors
======================================================
V2 normally places orders through executors (PositionExecutor / OrderExecutor),
one order at a time via `connector.buy()/sell()`. The Sera connector only groups
orders into a VL batch inside `batch_order_create()` (it never sees a batch if
orders arrive one-by-one), and it cancels the whole VL group atomically when any
leg is cancelled (`sera_exchange.py` `_place_cancel` -> `_cancel_vl_batch`). The
standard executor path therefore cannot produce — or cleanly tear down — VL
batches.

To stay faithful to V1 (which calls `market.batch_order_create(...)` directly),
this controller reaches the live connector through
`self.market_data_provider.connectors[...]`, calls `batch_order_create()` /
`cancel()` itself, and returns an empty executor-action list. Fill/PnL
accounting is done in the controller (mirroring V1, which also accounts
in-strategy) and surfaced via `update_processed_data()`, `get_custom_info()` and
`to_format_status()` rather than through the executor framework.

Reuse vs reimplement (see SERA_VL_CONTROLLER.md)
================================================
  - `MovingPriceBand` and `inventory_skew_calculator` are reused unchanged from
    `hummingbot/strategy/sera_market_making/`.
  - Budget constraint is a direct port of V1's manual running-balance loop
    (faithful to V1 and self-contained for the empty-book oracle case).
  - Hanging orders are reimplemented natively as group-aware logic, because the
    V1 `HangingOrdersTracker` assumes per-order cancellation with a per-order
    `OrderCancelled` event, which the connector's atomic VL group-cancel breaks.

Known fidelity notes
=====================
  - Fills are observed by polling `connector.in_flight_orders` each control-loop
    tick (~1s) rather than via V1's instant fill events, so ping-pong /
    filled_order_delay timing lags by up to one loop + the connector status poll
    interval. Accounting is driven off the connector order tracker (freshest
    source).
  - `price_source` external_market / custom_api delegates and the
    inventory_cost price delegate are not ported (rarely used with Sera).
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from pydantic import Field, field_validator

from hummingbot.core.data_type.common import MarketDict, OrderType, PriceType, TradeType
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.logger import HummingbotLogger
from hummingbot.strategy.sera_market_making.inventory_skew_calculator import (
    calculate_bid_ask_ratios_from_base_asset_ratio,
)
from hummingbot.strategy.sera_market_making.moving_price_band import MovingPriceBand
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction
from hummingbot.strategy_v2.utils.common import parse_comma_separated_list

s_decimal_zero = Decimal("0")
s_decimal_nan = Decimal("NaN")


class _PriceSize:
    """Mutable (price, size) leaf, mirroring V1's PriceSize so pipeline stages can edit in place."""

    __slots__ = ("price", "size")

    def __init__(self, price: Decimal, size: Decimal):
        self.price = price
        self.size = size

    def __repr__(self) -> str:
        return f"[price: {self.price}, size: {self.size}]"


class _Proposal:
    """Primary-pair proposal (lists of _PriceSize), mirroring V1's Proposal."""

    __slots__ = ("buys", "sells")

    def __init__(self, buys: List[_PriceSize], sells: List[_PriceSize]):
        self.buys = buys
        self.sells = sells


class _OrderView:
    """Lightweight read view of a live tracked order (built from the connector's InFlightOrder)."""

    __slots__ = ("client_order_id", "trading_pair", "is_buy", "price", "quantity", "age")

    def __init__(self, client_order_id, trading_pair, is_buy, price, quantity, age):
        self.client_order_id = client_order_id
        self.trading_pair = trading_pair
        self.is_buy = is_buy
        self.price = price
        self.quantity = quantity
        self.age = age


class _CreatedPair:
    """A bid/sell pair created in the same refresh, for hanging-order bookkeeping."""

    __slots__ = ("buy_id", "sell_id", "filled_buy", "filled_sell")

    def __init__(self, buy_id: Optional[str], sell_id: Optional[str]):
        self.buy_id = buy_id
        self.sell_id = sell_id
        self.filled_buy = False
        self.filled_sell = False


class SeraVLControllerConfig(ControllerConfigBase):
    """
    Config for the Sera VL market-making controller.

    Spread/amount semantics intentionally match the V1 config so an existing
    `conf_serapmm_sera.yml` maps over directly:
      - spreads are PERCENTS (0.25 == 0.25%)
      - amounts are BASE-asset amounts

    Ladder modes (mirror V1):
      - split_order_levels_enabled (default True): use the per-level
        `bid_spreads`/`ask_spreads` + `bid_amounts`/`ask_amounts` lists.
      - else: simple ladder from `bid_spread`/`ask_spread`, `order_amount`,
        `order_levels`, `order_level_spread`, `order_level_amount`.
      - `order_override` (a dict of {key: [side, spread_pct, size]}) takes
        priority over both, exactly like V1.
    """
    controller_name: str = "sera_vl"
    controller_type: str = "generic"

    connector_name: str = Field(
        default="sera",
        json_schema_extra={"prompt": "Connector name: ", "prompt_on_new": True},
    )
    # Primary / source market. Ladder prices are computed off this pair's reference price.
    trading_pair: str = Field(
        default="XSGD-MYRT",
        json_schema_extra={"prompt": "Primary trading pair (e.g. XSGD-MYRT): ", "prompt_on_new": True},
    )

    # --- Ladder: split-order-levels mode (V1 split_order_levels_enabled) ---
    split_order_levels_enabled: bool = Field(default=True)
    # PERCENT spreads from reference; one entry per level.
    bid_spreads: List[Decimal] = Field(default=[Decimal("0.25"), Decimal("0.5"), Decimal("1")])
    ask_spreads: List[Decimal] = Field(default=[Decimal("0.25"), Decimal("0.5"), Decimal("1")])
    # BASE-asset amounts; one entry per level.
    bid_amounts: List[Decimal] = Field(default=[Decimal("100"), Decimal("200"), Decimal("300")])
    ask_amounts: List[Decimal] = Field(default=[Decimal("100"), Decimal("200"), Decimal("300")])

    # --- Ladder: simple mode (used when split_order_levels_enabled is False) ---
    bid_spread: Decimal = Field(default=Decimal("0.25"))
    ask_spread: Decimal = Field(default=Decimal("0.25"))
    order_amount: Decimal = Field(default=Decimal("100"))
    order_levels: int = Field(default=1)
    order_level_spread: Decimal = Field(default=Decimal("1"))
    order_level_amount: Decimal = Field(default=Decimal("0"))
    # {key: [side, spread_pct, size]} — overrides both ladder modes when set.
    order_override: Optional[Dict[str, list]] = Field(default=None)

    # --- Refresh / aging ---
    order_refresh_time: float = Field(
        default=330.0,
        json_schema_extra={"prompt": "Order refresh time (seconds): ", "is_updatable": True},
    )
    max_order_age: float = Field(default=1800.0)
    order_refresh_tolerance_pct: Decimal = Field(default=Decimal("0"))
    filled_order_delay: float = Field(default=60.0)
    minimum_spread: Decimal = Field(default=Decimal("-100"))
    should_wait_order_cancel_confirmation: bool = Field(default=True)

    # --- Price bands ---
    price_ceiling: Decimal = Field(default=Decimal("-1"))
    price_floor: Decimal = Field(default=Decimal("-1"))
    moving_price_band_enabled: bool = Field(default=False)
    price_ceiling_pct: Decimal = Field(default=Decimal("1"))
    price_floor_pct: Decimal = Field(default=Decimal("-1"))
    price_band_refresh_time: float = Field(default=86400.0)

    # --- Ping pong ---
    ping_pong_enabled: bool = Field(default=False)

    # --- Order optimization (best bid/ask jumping) ---
    order_optimization_enabled: bool = Field(default=False)
    bid_order_optimization_depth: Decimal = Field(default=Decimal("0"))
    ask_order_optimization_depth: Decimal = Field(default=Decimal("0"))

    # --- Transaction costs ---
    add_transaction_costs: bool = Field(default=False)

    # --- Inventory skew ---
    inventory_skew_enabled: bool = Field(default=False)
    inventory_target_base_pct: Decimal = Field(default=Decimal("50"))
    inventory_range_multiplier: Decimal = Field(default=Decimal("1"))

    # --- Hanging orders ---
    hanging_orders_enabled: bool = Field(default=False)
    hanging_orders_cancel_pct: Decimal = Field(default=Decimal("10"))

    # --- Reference price ---
    # mid_price | last_price | best_bid | best_ask | last_own_trade_price
    price_type: str = Field(default="mid_price")
    take_if_crossed: bool = Field(default=True)

    # --- VL settings (mirror V1 use_vl_orders / vl_order_markets / vl_triangular_enabled) ---
    use_vl_orders: bool = Field(default=True)
    # Full list of VL markets INCLUDING the primary pair, matching V1 `vl_order_markets`.
    # The primary pair is skipped when mirroring (it is the source).
    vl_order_markets: List[str] = Field(default=["XSGD-MYRT", "XSGD-EGBP", "EGBP-MYRT"])
    vl_triangular_enabled: bool = Field(default=True)

    @field_validator("bid_spreads", "ask_spreads", "bid_amounts", "ask_amounts", mode="before")
    @classmethod
    def _parse_decimal_list(cls, v):
        if isinstance(v, str):
            return [Decimal(str(x)) for x in parse_comma_separated_list(v)]
        if isinstance(v, list):
            return [Decimal(str(x)) for x in v]
        return v

    @field_validator("vl_order_markets", mode="before")
    @classmethod
    def _parse_markets(cls, v):
        if isinstance(v, str):
            return [m.strip() for m in v.split(",") if m.strip()]
        return v

    def update_markets(self, markets: MarketDict) -> MarketDict:
        # Subscribe the connector to the primary pair and every VL sibling so the
        # MarketDataProvider has order books + a live connector instance for each.
        markets = markets.add_or_update(self.connector_name, self.trading_pair)
        for pair in self.vl_order_markets:
            markets = markets.add_or_update(self.connector_name, pair)
        return markets


class SeraVLController(ControllerBase):
    _logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self, config: SeraVLControllerConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        # V1-style timers (seconds, absolute). 0 => act on the first ready tick.
        self._create_timestamp: float = 0.0
        self._cancel_timestamp: float = 0.0

        # Fill-driven state (ported from V1).
        self._filled_base_balance: Decimal = s_decimal_zero
        self._filled_buys_balance: int = 0
        self._filled_sells_balance: int = 0
        self._last_own_trade_price: Optional[Decimal] = None
        self._ping_pong_warning_lines: List[str] = []

        # Orders we placed: client_order_id -> trading_pair.
        self._active_orders: Dict[str, str] = {}
        # Orders we have requested to cancel but have not yet observed as gone.
        self._in_flight_cancels: set = set()

        # Hanging-order bookkeeping (group/pair aware, native reimplementation).
        self._created_pairs: List[_CreatedPair] = []
        self._hanging_order_ids: set = set()

        # Reconciliation / accounting state.
        self._seen_trade_ids: set = set()
        self._completed_order_ids: set = set()
        self._accounting: Dict[str, Decimal] = {}

        self._moving_price_band = MovingPriceBand(
            price_floor_pct=config.price_floor_pct,
            price_ceiling_pct=config.price_ceiling_pct,
            price_band_refresh_time=config.price_band_refresh_time,
            enabled=config.moving_price_band_enabled,
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _split_pair(trading_pair: str) -> Tuple[str, str]:
        base, quote = trading_pair.split("-")
        return base, quote

    @property
    def _connector(self):
        """Live trading connector instance (used to fire VL batches directly)."""
        return self.market_data_provider.connectors.get(self.config.connector_name)

    def _now(self) -> float:
        return self.market_data_provider.time()

    def _q_price(self, trading_pair: str, price: Decimal) -> Decimal:
        connector = self._connector
        if connector is not None:
            return connector.quantize_order_price(trading_pair, price)
        return price

    def _q_amount(self, trading_pair: str, amount: Decimal) -> Decimal:
        connector = self._connector
        if connector is not None:
            return connector.quantize_order_amount(trading_pair, amount)
        return amount

    def _price_by_type(self, trading_pair: str, price_type: PriceType) -> Optional[Decimal]:
        price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, trading_pair, price_type)
        if price is None or Decimal(price).is_nan() or Decimal(price) <= 0:
            return None
        return Decimal(price)

    def _mid(self, trading_pair: str) -> Optional[Decimal]:
        # Routes through Sera's get_price_by_type(MidPrice), which falls back to
        # the Wise oracle mid when the book is empty (sera_exchange.py).
        return self._price_by_type(trading_pair, PriceType.MidPrice)

    def _reference_price(self) -> Optional[Decimal]:
        """V1 get_price(): the configured price_type for the primary pair, mid as fallback."""
        pt = (self.config.price_type or "mid_price").lower()
        if pt == "last_own_trade_price":
            if self._last_own_trade_price and self._last_own_trade_price > 0:
                return self._last_own_trade_price
            return self._mid(self.config.trading_pair)
        mapping = {
            "mid_price": PriceType.MidPrice,
            "best_bid": PriceType.BestBid,
            "best_ask": PriceType.BestAsk,
            "last_price": PriceType.LastTrade,
        }
        price = self._price_by_type(self.config.trading_pair, mapping.get(pt, PriceType.MidPrice))
        if price is None:
            return self._mid(self.config.trading_pair)
        return price

    # --- VL math (ports of c_vl_order_price / c_vl_order_amount) ---
    def _vl_order_price(self, target_pair: str, source_price: Decimal,
                        source_mid: Decimal) -> Optional[Decimal]:
        """Port of c_vl_order_price: scale sibling price by the source's price multiplier."""
        target_mid = self._mid(target_pair)
        if target_mid is None or source_mid is None or source_mid <= 0:
            return None
        multiplier = source_price / source_mid
        return self._q_price(target_pair, target_mid * multiplier)

    def _vl_order_amount(self, target_pair: str, source_amount: Decimal,
                         source_price: Decimal, target_price: Optional[Decimal]) -> Optional[Decimal]:
        """Port of c_vl_order_amount: preserve the quantity of the shared asset.

        Base model: only siblings whose BASE == primary base are eligible.
        Triangular model: any sibling sharing base or quote (either role) is eligible.
        """
        base_p, quote_p = self._split_pair(self.config.trading_pair)
        base_t, quote_t = self._split_pair(target_pair)

        if base_t == base_p:                                   # shared base in sibling base
            target_amount = source_amount
        elif not self.config.vl_triangular_enabled:
            return None
        elif target_price is None or target_price <= 0:
            return None
        elif quote_t == base_p:                                # shared = primary base, sibling quote
            target_amount = source_amount / target_price
        elif base_t == quote_p:                                # shared = primary quote, sibling base
            target_amount = source_amount * source_price
        elif quote_t == quote_p:                               # shared = primary quote, sibling quote
            target_amount = (source_amount * source_price) / target_price
        else:
            return None

        return self._q_amount(target_pair, target_amount)

    # ------------------------------------------------------------- active orders
    def _live_orders(self, primary_only: bool = False, exclude_hanging: bool = False) -> List[_OrderView]:
        """Build read views of our live (open) tracked orders from the connector."""
        connector = self._connector
        if connector is None:
            return []
        now = self._now()
        in_flight = getattr(connector, "in_flight_orders", {}) or {}
        views: List[_OrderView] = []
        for cid, pair in self._active_orders.items():
            if exclude_hanging and cid in self._hanging_order_ids:
                continue
            if primary_only and pair != self.config.trading_pair:
                continue
            ifo = in_flight.get(cid)
            if ifo is None or not ifo.is_open:
                continue
            views.append(_OrderView(
                client_order_id=cid,
                trading_pair=ifo.trading_pair,
                is_buy=ifo.trade_type == TradeType.BUY,
                price=ifo.price,
                quantity=ifo.amount,
                age=max(0.0, now - float(ifo.creation_timestamp)),
            ))
        return views

    def _adjusted_available_balance(self, orders: List[_OrderView]) -> Tuple[Decimal, Decimal]:
        """Port of c_get_adjusted_available_balance: available balance plus amounts locked in `orders`."""
        connector = self._connector
        base, quote = self._split_pair(self.config.trading_pair)
        base_balance = Decimal(str(connector.get_available_balance(base)))
        quote_balance = Decimal(str(connector.get_available_balance(quote)))
        for order in orders:
            if order.is_buy:
                quote_balance += order.quantity * order.price
            else:
                base_balance += order.quantity
        return base_balance, quote_balance

    # ----------------------------------------------------------- proposal stages
    def _create_base_proposal(self) -> Optional[_Proposal]:
        """Port of c_create_base_proposal. Returns primary-pair buys/sells, or None if no price."""
        ref = self._reference_price()
        if ref is None:
            self.logger().info(f"No reference price for {self.config.trading_pair} yet; skipping cycle.")
            return None

        pair = self.config.trading_pair
        buys: List[_PriceSize] = []
        sells: List[_PriceSize] = []

        override = self.config.order_override
        if override:
            for value in override.values():
                if not value or str(value[0]) not in ("buy", "sell"):
                    continue
                side, spread_pct, size = str(value[0]), Decimal(str(value[1])), Decimal(str(value[2]))
                if side == "buy":
                    price = self._q_price(pair, ref * (Decimal("1") - spread_pct / Decimal("100")))
                else:
                    price = self._q_price(pair, ref * (Decimal("1") + spread_pct / Decimal("100")))
                size = self._q_amount(pair, size)
                if size > 0 and price > 0:
                    (buys if side == "buy" else sells).append(_PriceSize(price, size))
            return _Proposal(buys, sells)

        if self.config.split_order_levels_enabled:
            for spread_pct, amount in zip(self.config.bid_spreads, self.config.bid_amounts):
                price = self._q_price(pair, ref * (Decimal("1") - spread_pct / Decimal("100")))
                size = self._q_amount(pair, amount)
                if size > 0:
                    buys.append(_PriceSize(price, size))
            for spread_pct, amount in zip(self.config.ask_spreads, self.config.ask_amounts):
                price = self._q_price(pair, ref * (Decimal("1") + spread_pct / Decimal("100")))
                size = self._q_amount(pair, amount)
                if size > 0:
                    sells.append(_PriceSize(price, size))
            return _Proposal(buys, sells)

        # Simple ladder mode.
        bid_spread = self.config.bid_spread / Decimal("100")
        ask_spread = self.config.ask_spread / Decimal("100")
        level_spread = self.config.order_level_spread / Decimal("100")
        for level in range(self.config.order_levels):
            price = self._q_price(pair, ref * (Decimal("1") - bid_spread - (level * level_spread)))
            size = self._q_amount(pair, self.config.order_amount + (self.config.order_level_amount * level))
            if size > 0:
                buys.append(_PriceSize(price, size))
        for level in range(self.config.order_levels):
            price = self._q_price(pair, ref * (Decimal("1") + ask_spread + (level * level_spread)))
            size = self._q_amount(pair, self.config.order_amount + (self.config.order_level_amount * level))
            if size > 0:
                sells.append(_PriceSize(price, size))
        return _Proposal(buys, sells)

    def _apply_order_levels_modifiers(self, proposal: _Proposal):
        self._apply_price_band(proposal)
        if self.config.moving_price_band_enabled:
            self._apply_moving_price_band(proposal)
        if self.config.ping_pong_enabled:
            self._apply_ping_pong(proposal)

    def _apply_price_band(self, proposal: _Proposal):
        ref = self._reference_price()
        if ref is None:
            return
        if self.config.price_ceiling > 0 and ref >= self.config.price_ceiling:
            proposal.buys = []
        if self.config.price_floor > 0 and ref <= self.config.price_floor:
            proposal.sells = []

    def _apply_moving_price_band(self, proposal: _Proposal):
        ref = self._reference_price()
        if ref is None:
            return
        self._moving_price_band.check_and_update_price_band(self._now(), ref)
        if self._moving_price_band.check_price_ceiling_exceeded(ref):
            proposal.buys = []
        if self._moving_price_band.check_price_floor_exceeded(ref):
            proposal.sells = []

    def _apply_ping_pong(self, proposal: _Proposal):
        self._ping_pong_warning_lines = []
        if self._filled_buys_balance == self._filled_sells_balance:
            self._filled_buys_balance = self._filled_sells_balance = 0
        if self._filled_buys_balance > 0:
            proposal.buys = proposal.buys[self._filled_buys_balance:]
            self._ping_pong_warning_lines.append(f"  Ping-pong removed {self._filled_buys_balance} buy orders.")
        if self._filled_sells_balance > 0:
            proposal.sells = proposal.sells[self._filled_sells_balance:]
            self._ping_pong_warning_lines.append(f"  Ping-pong removed {self._filled_sells_balance} sell orders.")

    def _apply_order_price_modifiers(self, proposal: _Proposal):
        if self.config.order_optimization_enabled:
            self._apply_order_optimization(proposal)
        if self.config.add_transaction_costs:
            self._apply_add_transaction_costs(proposal)

    def _apply_order_optimization(self, proposal: _Proposal):
        """Port of c_apply_order_optimization (best bid/ask jumping)."""
        connector = self._connector
        pair = self.config.trading_pair
        own_buy_size = max((o.quantity for o in self._live_orders(primary_only=True) if o.is_buy), default=s_decimal_zero)
        own_sell_size = max((o.quantity for o in self._live_orders(primary_only=True) if not o.is_buy), default=s_decimal_zero)

        if proposal.buys:
            top_bid = connector.get_price_for_volume(
                pair, False, self.config.bid_order_optimization_depth + own_buy_size).result_price
            if top_bid is not None and not Decimal(top_bid).is_nan():
                quantum = connector.get_order_price_quantum(pair, top_bid)
                price_above_bid = (Decimal(top_bid) // quantum + 1) * quantum
                proposal.buys = sorted(proposal.buys, key=lambda p: p.price, reverse=True)
                lower_buy_price = min(proposal.buys[0].price, price_above_bid)
                base_q = self._q_price(pair, lower_buy_price)
                for i, proposed in enumerate(proposal.buys):
                    if self.config.split_order_levels_enabled:
                        spreads = self.config.bid_spreads
                        proposed.price = (base_q * (Decimal("1") - spreads[i] / Decimal("100"))
                                          / (Decimal("1") - spreads[0] / Decimal("100")))
                    else:
                        proposed.price = base_q * (Decimal("1") - (self.config.order_level_spread / Decimal("100")) * i)

        if proposal.sells:
            top_ask = connector.get_price_for_volume(
                pair, True, self.config.ask_order_optimization_depth + own_sell_size).result_price
            if top_ask is not None and not Decimal(top_ask).is_nan():
                quantum = connector.get_order_price_quantum(pair, top_ask)
                price_below_ask = (Decimal(top_ask) // quantum - 1) * quantum
                proposal.sells = sorted(proposal.sells, key=lambda p: p.price)
                higher_sell_price = max(proposal.sells[0].price, price_below_ask)
                base_q = self._q_price(pair, higher_sell_price)
                for i, proposed in enumerate(proposal.sells):
                    if self.config.split_order_levels_enabled:
                        spreads = self.config.ask_spreads
                        proposed.price = (base_q * (Decimal("1") + spreads[i] / Decimal("100"))
                                          / (Decimal("1") + spreads[0] / Decimal("100")))
                    else:
                        proposed.price = base_q * (Decimal("1") + (self.config.order_level_spread / Decimal("100")) * i)

    def _apply_add_transaction_costs(self, proposal: _Proposal):
        """Port of c_apply_add_transaction_costs."""
        connector = self._connector
        base, quote = self._split_pair(self.config.trading_pair)
        for buy in proposal.buys:
            fee = connector.get_fee(base, quote, OrderType.LIMIT, TradeType.BUY, buy.size, buy.price, is_maker=True)
            buy.price = self._q_price(self.config.trading_pair, buy.price * (Decimal("1") - fee.percent))
        for sell in proposal.sells:
            fee = connector.get_fee(base, quote, OrderType.LIMIT, TradeType.SELL, sell.size, sell.price, is_maker=True)
            sell.price = self._q_price(self.config.trading_pair, sell.price * (Decimal("1") + fee.percent))

    def _apply_order_size_modifiers(self, proposal: _Proposal):
        if self.config.inventory_skew_enabled:
            self._apply_inventory_skew(proposal)
        self._apply_filled_base_balance(proposal)

    def _total_order_size(self) -> Decimal:
        if self.config.split_order_levels_enabled:
            return sum(self.config.bid_amounts, s_decimal_zero) + sum(self.config.ask_amounts, s_decimal_zero)
        levels = Decimal(self.config.order_levels)
        return Decimal("2") * (levels * self.config.order_amount
                               + levels * (levels - Decimal("1")) / Decimal("2") * self.config.order_level_amount)

    def _apply_inventory_skew(self, proposal: _Proposal):
        """Port of c_apply_inventory_skew (reuses inventory_skew_calculator)."""
        ref = self._reference_price()
        if ref is None or ref <= 0:
            return
        base_balance, quote_balance = self._adjusted_available_balance(
            self._live_orders(primary_only=True, exclude_hanging=True))
        base_asset_range = self._total_order_size() * self.config.inventory_range_multiplier
        ratios = calculate_bid_ask_ratios_from_base_asset_ratio(
            float(base_balance),
            float(quote_balance),
            float(ref),
            float(self.config.inventory_target_base_pct) / 100.0,
            float(base_asset_range),
        )
        bid_ratio = Decimal(str(ratios.bid_ratio))
        ask_ratio = Decimal(str(ratios.ask_ratio))
        for buy in proposal.buys:
            buy.size = self._q_amount(self.config.trading_pair, buy.size * bid_ratio)
        for sell in proposal.sells:
            sell.size = self._q_amount(self.config.trading_pair, sell.size * ask_ratio)

    def _apply_filled_base_balance(self, proposal: _Proposal):
        """Port of c_apply_filled_base_balance: route accumulated inventory imbalance into the top order."""
        pair = self.config.trading_pair
        mid = self._mid(pair)
        if self._filled_base_balance > s_decimal_zero and proposal.sells:
            if mid is not None:
                proposal.sells[0].price = self._q_price(pair, mid)
            proposal.sells[0].size = self._q_amount(pair, proposal.sells[0].size + self._filled_base_balance)
        elif self._filled_base_balance < s_decimal_zero and proposal.buys:
            if mid is not None:
                proposal.buys[0].price = self._q_price(pair, mid)
            proposal.buys[0].size = self._q_amount(pair, proposal.buys[0].size - self._filled_base_balance)

    def _apply_budget_constraint(self, proposal: _Proposal):
        """Port of c_apply_budget_constraint (manual running-balance loop, faithful to V1).

        Constrains the PRIMARY-pair ladder; sibling legs are sized off the primary via the
        VL math, so constraining the primary cascades. (`connector.budget_checker.adjust_candidates`
        is the V2-native alternative but does not reason across the shared assets VL siblings draw on.)
        """
        connector = self._connector
        base, quote = self._split_pair(self.config.trading_pair)
        base_balance, quote_balance = self._adjusted_available_balance(
            self._live_orders(primary_only=True, exclude_hanging=True))

        for buy in proposal.buys:
            fee = connector.get_fee(base, quote, OrderType.LIMIT, TradeType.BUY, buy.size, buy.price, is_maker=True)
            quote_size = buy.size * buy.price * (Decimal("1") + fee.percent)
            if quote_balance < quote_size:
                adjusted = quote_balance / (buy.price * (Decimal("1") + fee.percent)) if buy.price > 0 else s_decimal_zero
                buy.size = self._q_amount(self.config.trading_pair, adjusted)
                quote_balance = s_decimal_zero
            elif quote_balance == s_decimal_zero:
                buy.size = s_decimal_zero
            else:
                quote_balance -= quote_size
        proposal.buys = [o for o in proposal.buys if o.size > 0]

        for sell in proposal.sells:
            if base_balance < sell.size:
                sell.size = self._q_amount(self.config.trading_pair, base_balance)
                base_balance = s_decimal_zero
            elif base_balance == s_decimal_zero:
                sell.size = s_decimal_zero
            else:
                base_balance -= sell.size
        proposal.sells = [o for o in proposal.sells if o.size > 0]

    def _filter_out_takers(self, proposal: _Proposal):
        """Port of c_filter_out_takers."""
        pair = self.config.trading_pair
        top_ask = self._price_by_type(pair, PriceType.BestAsk)
        if top_ask is not None:
            proposal.buys = [b for b in proposal.buys if b.price < top_ask]
        top_bid = self._price_by_type(pair, PriceType.BestBid)
        if top_bid is not None:
            proposal.sells = [s for s in proposal.sells if s.price > top_bid]

    # --------------------------------------------------------------- VL batching
    def _proposal_to_legs(self, proposal: _Proposal) -> List[LimitOrder]:
        """Faithful to c_execute_orders_proposal_as_batch leg construction.

        For each primary-pair order, add a sibling order on each VL market (skipping the primary).
        """
        ref = self._mid(self.config.trading_pair)
        if ref is None:
            return []
        base_p, quote_p = self._split_pair(self.config.trading_pair)
        legs: List[LimitOrder] = []

        def add_side(orders: List[_PriceSize], is_buy: bool):
            for ps in orders:
                legs.append(LimitOrder(
                    client_order_id="",
                    trading_pair=self.config.trading_pair,
                    is_buy=is_buy,
                    base_currency=base_p,
                    quote_currency=quote_p,
                    price=ps.price,
                    quantity=ps.size,
                ))
                if not self.config.use_vl_orders:
                    continue
                for sibling in self.config.vl_order_markets:
                    if sibling == self.config.trading_pair:
                        continue
                    sib_price = self._vl_order_price(sibling, ps.price, ref)
                    if sib_price is None:
                        continue
                    sib_amount = self._vl_order_amount(sibling, ps.size, ps.price, sib_price)
                    if sib_amount is None or sib_amount <= 0:
                        continue
                    base_s, quote_s = self._split_pair(sibling)
                    legs.append(LimitOrder(
                        client_order_id="",
                        trading_pair=sibling,
                        is_buy=is_buy,
                        base_currency=base_s,
                        quote_currency=quote_s,
                        price=sib_price,
                        quantity=sib_amount,
                    ))

        add_side(proposal.buys, is_buy=True)
        add_side(proposal.sells, is_buy=False)
        return legs

    # ---------------------------------------------------------------- cancelling
    def _cancel_order(self, view: _OrderView):
        connector = self._connector
        if connector is None:
            return
        # The Sera connector batch-cancels the whole VL group when any leg is
        # cancelled, so cancelling each tracked leg is safe and idempotent.
        connector.cancel(view.trading_pair, view.client_order_id)
        self._in_flight_cancels.add(view.client_order_id)

    def _cancel_active_orders_on_max_age_limit(self):
        """Port of c_cancel_active_orders_on_max_age_limit (primary-pair, non-hanging)."""
        active = self._live_orders(primary_only=True, exclude_hanging=True)
        if active and any(o.age > self.config.max_order_age for o in active):
            for o in active:
                self._cancel_order(o)

    def _are_orders_within_tolerance(self, active_orders: List[_OrderView], proposal_orders: List[_PriceSize]) -> bool:
        """Port of c_are_orders_within_tolerance."""
        if len(active_orders) != len(proposal_orders):
            return False
        tol = self.config.order_refresh_tolerance_pct / Decimal("100")
        active_orders = sorted(active_orders, key=lambda o: o.price)
        proposal_orders = sorted(proposal_orders, key=lambda p: p.price)
        for active_order, proposal_order in zip(active_orders, proposal_orders):
            if active_order.price <= 0:
                return False
            if abs(proposal_order.price - active_order.price) / active_order.price > tol:
                return False
            if proposal_order.size != Decimal(str(active_order.quantity)):
                return False
        return True

    def _cancel_active_orders(self, proposal: Optional[_Proposal]):
        """Port of c_cancel_active_orders with order_refresh_tolerance_pct defer."""
        if self._cancel_timestamp > self._now():
            return
        active = self._live_orders(primary_only=True, exclude_hanging=True)
        if not active:
            return

        to_defer = False
        if proposal is not None and self.config.order_refresh_tolerance_pct >= 0:
            active_buys = [o for o in active if o.is_buy]
            active_sells = [o for o in active if not o.is_buy]
            if (self._are_orders_within_tolerance(active_buys, proposal.buys)
                    and self._are_orders_within_tolerance(active_sells, proposal.sells)):
                to_defer = True

        if to_defer:
            self.set_timers()
            return
        for o in self._live_orders(exclude_hanging=True):
            if not self._is_potential_hanging_order(o.client_order_id):
                self._cancel_order(o)

    def _cancel_orders_below_min_spread(self):
        """Port of c_cancel_orders_below_min_spread."""
        ref = self._reference_price()
        if ref is None or ref <= 0:
            return
        min_spread = self.config.minimum_spread / Decimal("100")
        for o in self._live_orders(primary_only=True, exclude_hanging=True):
            negation = Decimal("-1") if o.is_buy else Decimal("1")
            if (negation * (o.price - ref) / ref) < min_spread:
                self.logger().info(
                    f"Order below minimum spread ({self.config.minimum_spread}). Canceling "
                    f"{'Buy' if o.is_buy else 'Sell'} {o.client_order_id}")
                self._cancel_order(o)

    # ------------------------------------------------------------ hanging orders
    def _is_potential_hanging_order(self, client_order_id: str) -> bool:
        return self.config.hanging_orders_enabled and client_order_id in self._hanging_order_ids

    def _hanging_process_tick(self):
        """Native group-aware port of HangingOrdersTracker.process_tick.

        Cancels hanging orders that drift past hanging_orders_cancel_pct from mid or exceed
        max_order_age (renewal then happens through the normal create cycle). VL group-cancel
        semantics mean cancelling a hanging leg also tears down its group on the exchange.
        """
        if not self.config.hanging_orders_enabled:
            self._hanging_order_ids.clear()
            return
        mid = self._mid(self.config.trading_pair)
        live_ids = {o.client_order_id: o for o in self._live_orders()}
        cancel_pct = self.config.hanging_orders_cancel_pct / Decimal("100")
        for cid in list(self._hanging_order_ids):
            view = live_ids.get(cid)
            if view is None:
                self._hanging_order_ids.discard(cid)
                continue
            if mid is not None and mid > 0 and abs(view.price - mid) / mid > cancel_pct:
                self._cancel_order(view)
                self._hanging_order_ids.discard(cid)
            elif view.age > self.config.max_order_age:
                self._cancel_order(view)
                self._hanging_order_ids.discard(cid)

    # --------------------------------------------------------------------- gates
    def _to_create_orders(self, proposal: Optional[_Proposal]) -> bool:
        """Port of c_to_create_orders (honours should_wait_order_cancel_confirmation)."""
        non_hanging_non_cancelled = [
            o for o in self._live_orders(primary_only=True, exclude_hanging=True)
            if o.client_order_id not in self._in_flight_cancels
        ]
        return (self._create_timestamp < self._now()
                and (not self.config.should_wait_order_cancel_confirmation
                     or len(self._in_flight_cancels) == 0)
                and proposal is not None
                and len(non_hanging_non_cancelled) == 0)

    def set_timers(self):
        """Port of set_timers."""
        now = self._now()
        next_cycle = now + self.config.order_refresh_time
        if self._create_timestamp <= now:
            self._create_timestamp = next_cycle
        if self._cancel_timestamp <= now:
            self._cancel_timestamp = min(self._create_timestamp, next_cycle)

    def _execute_orders_proposal(self, proposal: _Proposal):
        """Port of c_execute_orders_proposal_as_batch: build legs, submit one VL batch, track."""
        connector = self._connector
        legs = self._proposal_to_legs(proposal)
        if not legs:
            return
        created = connector.batch_order_create(legs)
        if not created:
            return

        number_of_pairs = min(len(proposal.buys), len(proposal.sells)) if self.config.hanging_orders_enabled else 0
        primary_buys = [o for o in created if o.trading_pair == self.config.trading_pair and o.is_buy]
        primary_sells = [o for o in created if o.trading_pair == self.config.trading_pair and not o.is_buy]
        self._created_pairs = []
        for i in range(number_of_pairs):
            buy_id = primary_buys[i].client_order_id if i < len(primary_buys) else None
            sell_id = primary_sells[i].client_order_id if i < len(primary_sells) else None
            self._created_pairs.append(_CreatedPair(buy_id, sell_id))

        for order in created:
            self._active_orders[order.client_order_id] = order.trading_pair

        self.set_timers()
        self.logger().info(
            f"Placed {len(created)} Sera VL legs across "
            f"{len({o.trading_pair for o in created})} markets.")

    # ------------------------------------------------ reconciliation & accounting
    def _reconcile_and_account(self):
        """Prune dead orders, detect fills/completions (drives timers, ping-pong, accounting)."""
        connector = self._connector
        if connector is None:
            return
        in_flight = getattr(connector, "in_flight_orders", {}) or {}
        now = self._now()
        primary = self.config.trading_pair

        # Detect new fills and completions on orders we placed.
        for cid, pair in list(self._active_orders.items()):
            ifo = in_flight.get(cid)
            if ifo is None:
                # No longer tracked by the connector: treat as gone.
                self._active_orders.pop(cid, None)
                self._in_flight_cancels.discard(cid)
                continue
            # Per-trade accounting (all our pairs) + primary inventory delta.
            for trade_id, fill in list(getattr(ifo, "order_fills", {}).items()):
                if trade_id in self._seen_trade_ids:
                    continue
                self._seen_trade_ids.add(trade_id)
                is_buy = ifo.trade_type == TradeType.BUY
                self._accounting["fills"] = self._accounting.get("fills", s_decimal_zero) + Decimal("1")
                self._accounting["volume_quote"] = (
                    self._accounting.get("volume_quote", s_decimal_zero) + Decimal(str(fill.fill_quote_amount)))
                if pair == primary:
                    delta = Decimal(str(fill.fill_base_amount))
                    self._filled_base_balance += delta if is_buy else -delta
                    # V1 delays the next create cycle on every fill.
                    self._create_timestamp = now + self.config.filled_order_delay
                    self._cancel_timestamp = min(self._cancel_timestamp, self._create_timestamp)

            # Completion detection (drives ping-pong counters; primary, full fills only).
            if pair == primary and ifo.is_filled and cid not in self._completed_order_ids:
                self._completed_order_ids.add(cid)
                self._last_own_trade_price = ifo.price
                self._mark_pair_filled(cid, ifo.trade_type == TradeType.BUY)
                if not self._is_potential_hanging_order(cid):
                    if ifo.trade_type == TradeType.BUY:
                        self._filled_buys_balance += 1
                    else:
                        self._filled_sells_balance += 1

            if ifo.is_done:
                self._active_orders.pop(cid, None)
                self._in_flight_cancels.discard(cid)

        # Clear in-flight cancels for ids the connector no longer reports open.
        for cid in list(self._in_flight_cancels):
            ifo = in_flight.get(cid)
            if ifo is None or not ifo.is_open:
                self._in_flight_cancels.discard(cid)

        # Cumulative fees in the primary quote asset, summed across live tracked orders.
        _, quote = self._split_pair(primary)
        cum_fees = s_decimal_zero
        for cid in self._active_orders:
            ifo = in_flight.get(cid)
            if ifo is not None:
                cum_fees += ifo.cumulative_fee_paid(quote)
        self._accounting["open_fees_quote"] = cum_fees

    def _mark_pair_filled(self, client_order_id: str, is_buy: bool):
        """When one side of a created pair fully fills, the unfilled partner becomes hanging."""
        if not self.config.hanging_orders_enabled:
            return
        for pair in self._created_pairs:
            if is_buy and pair.buy_id == client_order_id:
                pair.filled_buy = True
                if pair.sell_id and not pair.filled_sell:
                    self._hanging_order_ids.add(pair.sell_id)
            elif (not is_buy) and pair.sell_id == client_order_id:
                pair.filled_sell = True
                if pair.buy_id and not pair.filled_buy:
                    self._hanging_order_ids.add(pair.buy_id)

    # ------------------------------------------------------------------ V2 hooks
    async def update_processed_data(self):
        # Reconcile + account first so the proposal pipeline (run in
        # determine_executor_actions) sees up-to-date fill state and timers.
        self._reconcile_and_account()
        self.processed_data = {
            "reference_price": self._reference_price(),
            "mid_price": self._mid(self.config.trading_pair),
            "active_order_count": len(self._active_orders),
            "hanging_order_count": len(self._hanging_order_ids),
            "in_flight_cancels": len(self._in_flight_cancels),
            "filled_base_balance": self._filled_base_balance,
            "filled_buys_balance": self._filled_buys_balance,
            "filled_sells_balance": self._filled_sells_balance,
            "fills": self._accounting.get("fills", s_decimal_zero),
            "volume_quote": self._accounting.get("volume_quote", s_decimal_zero),
            "open_fees_quote": self._accounting.get("open_fees_quote", s_decimal_zero),
        }

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """Run the full V1 tick pipeline. VL orders are placed directly on the
        connector (see module docstring), so this returns no executor actions."""
        connector = self._connector
        if connector is None or not getattr(connector, "ready", False):
            return []

        proposal: Optional[_Proposal] = None
        if self._create_timestamp <= self._now():
            proposal = self._create_base_proposal()
            if proposal is not None:
                self._apply_order_levels_modifiers(proposal)
                self._apply_order_price_modifiers(proposal)
                self._apply_order_size_modifiers(proposal)
                self._apply_budget_constraint(proposal)
                if not self.config.take_if_crossed:
                    self._filter_out_takers(proposal)

        self._hanging_process_tick()
        self._cancel_active_orders_on_max_age_limit()
        self._cancel_active_orders(proposal)
        self._cancel_orders_below_min_spread()
        if self._to_create_orders(proposal):
            self._execute_orders_proposal(proposal)
        return []

    def get_custom_info(self) -> dict:
        # Published over MQTT with the performance report (orders placed directly
        # on the connector are not visible to the executor-PnL panel).
        return {
            "reference_price": str(self.processed_data.get("reference_price")),
            "active_orders": self.processed_data.get("active_order_count", 0),
            "hanging_orders": self.processed_data.get("hanging_order_count", 0),
            "fills": str(self.processed_data.get("fills", 0)),
            "volume_quote": str(self.processed_data.get("volume_quote", 0)),
            "open_fees_quote": str(self.processed_data.get("open_fees_quote", 0)),
            "filled_base_balance": str(self._filled_base_balance),
            "vl_markets": self.config.vl_order_markets,
            "vl_triangular_enabled": self.config.vl_triangular_enabled,
        }

    def to_format_status(self) -> List[str]:
        ref = self.processed_data.get("reference_price")
        lines = [
            f"Sera VL controller | primary={self.config.trading_pair} ref={ref}",
            f"VL markets={self.config.vl_order_markets} triangular={self.config.vl_triangular_enabled}",
            f"active legs={len(self._active_orders)} hanging={len(self._hanging_order_ids)} "
            f"in_flight_cancels={len(self._in_flight_cancels)}",
            f"fills={self.processed_data.get('fills', 0)} volume_quote={self.processed_data.get('volume_quote', 0)} "
            f"open_fees_quote={self.processed_data.get('open_fees_quote', 0)}",
            f"filled_base_balance={self._filled_base_balance} "
            f"ping_pong(buys={self._filled_buys_balance}, sells={self._filled_sells_balance})",
        ]
        lines.extend(self._ping_pong_warning_lines)
        return lines

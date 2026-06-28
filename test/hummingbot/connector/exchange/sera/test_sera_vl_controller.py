import asyncio
import unittest
from decimal import Decimal
from typing import Dict

from controllers.generic.sera_vl import SeraVLController, SeraVLControllerConfig
from hummingbot.core.data_type.common import PriceType, TradeType
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee


class _FakeFill:
    def __init__(self, trade_id: str, base: Decimal, quote: Decimal):
        self.trade_id = trade_id
        self.fill_base_amount = base
        self.fill_quote_amount = quote


class _FakeInFlightOrder:
    def __init__(self, cid, trading_pair, trade_type, price, amount,
                 creation_timestamp=0.0, is_open=True, is_done=False, is_filled=False, fees=Decimal("0")):
        self.client_order_id = cid
        self.trading_pair = trading_pair
        self.trade_type = trade_type
        self.price = price
        self.amount = amount
        self.creation_timestamp = creation_timestamp
        self.is_open = is_open
        self.is_done = is_done
        self.is_filled = is_filled
        self.order_fills: Dict[str, _FakeFill] = {}
        self._fees = fees

    def cumulative_fee_paid(self, token):
        return self._fees


class _FakePriceVolumeResult:
    def __init__(self, result_price):
        self.result_price = result_price


class _FakeConnector:
    """Minimal stand-in for the Sera connector exercising only what the controller calls."""

    def __init__(self, mids: Dict[str, Decimal], balances: Dict[str, Decimal]):
        self.ready = True
        self._mids = mids
        self._balances = balances
        self.in_flight_orders: Dict[str, _FakeInFlightOrder] = {}
        self.created_batches = []      # list of leg-lists passed to batch_order_create
        self.cancelled = []            # list of (pair, cid)
        self._next_id = 0
        self.fee_percent = Decimal("0")

    # --- pricing ---
    def get_price_by_type(self, trading_pair, price_type):
        mid = self._mids.get(trading_pair)
        if mid is None:
            return Decimal("NaN")
        if price_type == PriceType.BestBid:
            return mid * Decimal("0.999")
        if price_type == PriceType.BestAsk:
            return mid * Decimal("1.001")
        return mid

    def get_price_for_volume(self, trading_pair, is_buy, volume):
        mid = self._mids.get(trading_pair, Decimal("1"))
        return _FakePriceVolumeResult(mid * (Decimal("1.001") if is_buy else Decimal("0.999")))

    def get_order_price_quantum(self, trading_pair, price):
        return Decimal("0.0001")

    # --- quantization (identity, for deterministic assertions) ---
    def quantize_order_price(self, trading_pair, price):
        return Decimal(price)

    def quantize_order_amount(self, trading_pair, amount):
        return Decimal(amount)

    # --- balances / fees ---
    def get_available_balance(self, asset):
        return self._balances.get(asset, Decimal("0"))

    def get_fee(self, base, quote, order_type, order_side, amount, price, is_maker=None):
        return AddedToCostTradeFee(percent=self.fee_percent)

    # --- order ops ---
    def batch_order_create(self, orders):
        self.created_batches.append(orders)
        created = []
        for o in orders:
            self._next_id += 1
            cid = f"cid-{self._next_id}"
            created.append(LimitOrder(
                client_order_id=cid,
                trading_pair=o.trading_pair,
                is_buy=o.is_buy,
                base_currency=o.base_currency,
                quote_currency=o.quote_currency,
                price=o.price,
                quantity=o.quantity,
            ))
            self.in_flight_orders[cid] = _FakeInFlightOrder(
                cid, o.trading_pair, TradeType.BUY if o.is_buy else TradeType.SELL, o.price, o.quantity)
        return created

    def cancel(self, trading_pair, client_order_id):
        self.cancelled.append((trading_pair, client_order_id))


class _FakeMarketDataProvider:
    def __init__(self, connector, name, now=1000.0):
        self.connectors = {name: connector}
        self._now = now

    def time(self):
        return self._now

    def get_price_by_type(self, connector_name, trading_pair, price_type):
        return self.connectors[connector_name].get_price_by_type(trading_pair, price_type)


def _make_controller(**overrides):
    mids = {
        "XSGD-MYRT": Decimal("3.5"),
        "XSGD-EGBP": Decimal("0.6"),
        "EGBP-MYRT": Decimal("5.8333"),
    }
    balances = {"XSGD": Decimal("1000000"), "MYRT": Decimal("1000000"),
                "EGBP": Decimal("1000000")}
    connector = _FakeConnector(mids, balances)
    cfg_kwargs = dict(id="t", connector_name="sera", trading_pair="XSGD-MYRT",
                      vl_order_markets=["XSGD-MYRT", "XSGD-EGBP", "EGBP-MYRT"])
    cfg_kwargs.update(overrides)
    config = SeraVLControllerConfig(**cfg_kwargs)
    mdp = _FakeMarketDataProvider(connector, "sera")
    controller = SeraVLController(config, mdp, asyncio.Queue())
    return controller, connector


class SeraVLControllerTest(unittest.TestCase):
    def setUp(self):
        self.controller, self.connector = _make_controller()

    # ---------------------------------------------------------------- VL math
    def test_vl_order_price_scales_by_multiplier(self):
        # source XSGD-MYRT mid 3.5, sibling XSGD-EGBP mid 0.6, source price 3.4825 (0.5% below mid)
        source_mid = Decimal("3.5")
        source_price = Decimal("3.4825")
        price = self.controller._vl_order_price("XSGD-EGBP", source_price, source_mid)
        expected = Decimal("0.6") * (source_price / source_mid)
        self.assertEqual(price, expected)

    def test_vl_order_amount_base_model_shares_base(self):
        # XSGD-EGBP shares base XSGD with primary XSGD-MYRT -> mirror amount.
        amount = self.controller._vl_order_amount("XSGD-EGBP", Decimal("100"), Decimal("3.48"), Decimal("0.59"))
        self.assertEqual(amount, Decimal("100"))

    def test_vl_order_amount_triangular_quote_in_sibling_base(self):
        # primary quote MYRT appears as sibling base in MYRT-USD -> amount = source_amount * source_price
        c, _ = _make_controller(vl_triangular_enabled=True,
                                vl_order_markets=["XSGD-MYRT", "MYRT-USD"])
        c.market_data_provider.connectors["sera"]._mids["MYRT-USD"] = Decimal("0.30")
        amount = c._vl_order_amount("MYRT-USD", Decimal("100"), Decimal("3.5"), Decimal("0.30"))
        self.assertEqual(amount, Decimal("100") * Decimal("3.5"))

    def test_vl_order_amount_triangular_disabled_rejects_non_base(self):
        c, _ = _make_controller(vl_triangular_enabled=False,
                                vl_order_markets=["XSGD-MYRT", "EGBP-MYRT"])
        # EGBP-MYRT shares only the quote (MYRT) -> rejected when triangular disabled.
        amount = c._vl_order_amount("EGBP-MYRT", Decimal("100"), Decimal("3.5"), Decimal("5.8"))
        self.assertIsNone(amount)

    # ----------------------------------------------------------- base proposal
    def test_create_base_proposal_split_levels(self):
        proposal = self.controller._create_base_proposal()
        self.assertEqual(len(proposal.buys), 3)
        self.assertEqual(len(proposal.sells), 3)
        # First bid is 0.25% below mid 3.5.
        self.assertEqual(proposal.buys[0].price, Decimal("3.5") * (Decimal("1") - Decimal("0.25") / Decimal("100")))
        self.assertEqual(proposal.buys[0].size, Decimal("100"))
        self.assertEqual(proposal.sells[0].price, Decimal("3.5") * (Decimal("1") + Decimal("0.25") / Decimal("100")))

    def test_create_base_proposal_simple_mode(self):
        c, _ = _make_controller(split_order_levels_enabled=False, order_levels=2,
                                bid_spread=Decimal("1"), ask_spread=Decimal("1"),
                                order_amount=Decimal("10"), order_level_amount=Decimal("5"),
                                order_level_spread=Decimal("1"))
        proposal = c._create_base_proposal()
        self.assertEqual(len(proposal.buys), 2)
        self.assertEqual(proposal.buys[0].size, Decimal("10"))
        self.assertEqual(proposal.buys[1].size, Decimal("15"))
        # second buy level: 1% + 1% below mid
        self.assertEqual(proposal.buys[1].price, Decimal("3.5") * (Decimal("1") - Decimal("0.01") - Decimal("0.01")))

    def test_create_base_proposal_order_override(self):
        c, _ = _make_controller(order_override={"o1": ["buy", 2, 50], "o2": ["sell", 3, 40]})
        proposal = c._create_base_proposal()
        self.assertEqual(len(proposal.buys), 1)
        self.assertEqual(len(proposal.sells), 1)
        self.assertEqual(proposal.buys[0].size, Decimal("50"))
        self.assertEqual(proposal.buys[0].price, Decimal("3.5") * (Decimal("1") - Decimal("2") / Decimal("100")))

    def test_create_base_proposal_no_price_returns_none(self):
        self.connector._mids["XSGD-MYRT"] = None
        self.assertIsNone(self.controller._create_base_proposal())

    # ---------------------------------------------------------- proposal->legs
    def test_proposal_to_legs_mirrors_to_all_vl_siblings(self):
        proposal = self.controller._create_base_proposal()
        legs = self.controller._proposal_to_legs(proposal)
        # 3 bids + 3 asks = 6 primary orders, each mirrored to 2 siblings -> 6 * 3 = 18 legs.
        self.assertEqual(len(legs), 18)
        pairs = {leg.trading_pair for leg in legs}
        self.assertEqual(pairs, {"XSGD-MYRT", "XSGD-EGBP", "EGBP-MYRT"})

    def test_proposal_to_legs_no_vl_when_disabled(self):
        c, _ = _make_controller(use_vl_orders=False)
        proposal = c._create_base_proposal()
        legs = c._proposal_to_legs(proposal)
        self.assertEqual(len(legs), 6)
        self.assertTrue(all(leg.trading_pair == "XSGD-MYRT" for leg in legs))

    # ------------------------------------------------------------- price bands
    def test_static_price_ceiling_removes_buys(self):
        c, _ = _make_controller(price_ceiling=Decimal("3.0"))  # mid 3.5 >= 3.0
        proposal = c._create_base_proposal()
        c._apply_price_band(proposal)
        self.assertEqual(proposal.buys, [])
        self.assertEqual(len(proposal.sells), 3)

    def test_static_price_floor_removes_sells(self):
        c, _ = _make_controller(price_floor=Decimal("4.0"))  # mid 3.5 <= 4.0
        proposal = c._create_base_proposal()
        c._apply_price_band(proposal)
        self.assertEqual(proposal.sells, [])

    def test_moving_price_band_removes_buys_above_ceiling(self):
        c, _ = _make_controller(moving_price_band_enabled=True,
                                price_ceiling_pct=Decimal("-1"), price_floor_pct=Decimal("-5"))
        proposal = c._create_base_proposal()
        c._apply_moving_price_band(proposal)
        # ceiling set to mid*(1-0.01) which is below mid -> buys removed.
        self.assertEqual(proposal.buys, [])

    # --------------------------------------------------------------- ping pong
    def test_ping_pong_removes_filled_side(self):
        c, _ = _make_controller(ping_pong_enabled=True)
        c._filled_buys_balance = 1
        proposal = c._create_base_proposal()
        c._apply_ping_pong(proposal)
        self.assertEqual(len(proposal.buys), 2)
        self.assertEqual(len(proposal.sells), 3)

    # ------------------------------------------------------------ transaction costs
    def test_add_transaction_costs_shifts_prices(self):
        c, conn = _make_controller(add_transaction_costs=True)
        conn.fee_percent = Decimal("0.001")
        proposal = c._create_base_proposal()
        original_buy = proposal.buys[0].price
        c._apply_add_transaction_costs(proposal)
        self.assertEqual(proposal.buys[0].price, original_buy * (Decimal("1") - Decimal("0.001")))

    # -------------------------------------------------------------- inventory skew
    def test_inventory_skew_scales_sizes(self):
        # base-heavy inventory -> bid ratio < 1 (buy less), ask ratio > 1 (sell more).
        c, conn = _make_controller(inventory_skew_enabled=True,
                                   inventory_target_base_pct=Decimal("50"))
        conn._balances["XSGD"] = Decimal("100000")
        conn._balances["MYRT"] = Decimal("100")
        proposal = c._create_base_proposal()
        bid_before = proposal.buys[0].size
        ask_before = proposal.sells[0].size
        c._apply_inventory_skew(proposal)
        self.assertLess(proposal.buys[0].size, bid_before)
        self.assertGreater(proposal.sells[0].size, ask_before)

    # --------------------------------------------------------- filled base balance
    def test_filled_base_balance_grows_first_sell(self):
        proposal = self.controller._create_base_proposal()
        self.controller._filled_base_balance = Decimal("20")
        before = proposal.sells[0].size
        self.controller._apply_filled_base_balance(proposal)
        self.assertEqual(proposal.sells[0].size, before + Decimal("20"))

    # ---------------------------------------------------------- budget constraint
    def test_budget_constraint_caps_sell_size(self):
        c, conn = _make_controller()
        conn._balances["XSGD"] = Decimal("150")   # only enough base for part of the 100+200+300 ask ladder
        conn._balances["MYRT"] = Decimal("100000000")
        proposal = c._create_base_proposal()
        c._apply_budget_constraint(proposal)
        total_sell = sum((s.size for s in proposal.sells), Decimal("0"))
        self.assertLessEqual(total_sell, Decimal("150"))

    def test_budget_constraint_caps_buy_size(self):
        c, conn = _make_controller()
        conn._balances["MYRT"] = Decimal("400")   # ~enough quote for first small bid only
        conn._balances["XSGD"] = Decimal("100000000")
        proposal = c._create_base_proposal()
        c._apply_budget_constraint(proposal)
        self.assertTrue(len(proposal.buys) < 3)

    # ------------------------------------------------------------- taker filter
    def test_filter_out_takers_removes_crossing_orders(self):
        c, _ = _make_controller()
        proposal = c._create_base_proposal()
        # Force a buy above best ask and a sell below best bid.
        proposal.buys[0].price = Decimal("100")
        proposal.sells[0].price = Decimal("0.0001")
        c._filter_out_takers(proposal)
        self.assertTrue(all(b.price < Decimal("3.5") * Decimal("1.001") for b in proposal.buys))
        self.assertTrue(all(s.price > Decimal("3.5") * Decimal("0.999") for s in proposal.sells))

    # ----------------------------------------------------------------- tolerance
    def test_are_orders_within_tolerance(self):
        from controllers.generic.sera_vl import _OrderView, _PriceSize
        c, _ = _make_controller(order_refresh_tolerance_pct=Decimal("1"))  # 1%
        active = [_OrderView("a", "XSGD-MYRT", True, Decimal("3.50"), Decimal("100"), 1.0)]
        within = [_PriceSize(Decimal("3.51"), Decimal("100"))]   # ~0.28% diff
        outside = [_PriceSize(Decimal("3.60"), Decimal("100"))]  # ~2.8% diff
        self.assertTrue(c._are_orders_within_tolerance(active, within))
        self.assertFalse(c._are_orders_within_tolerance(active, outside))

    # ------------------------------------------------------------- hanging orders
    def test_hanging_order_promoted_when_pair_partner_fills(self):
        from controllers.generic.sera_vl import _CreatedPair
        c, _ = _make_controller(hanging_orders_enabled=True)
        c._created_pairs = [_CreatedPair("buy1", "sell1")]
        c._mark_pair_filled("buy1", is_buy=True)
        self.assertIn("sell1", c._hanging_order_ids)
        self.assertTrue(c._is_potential_hanging_order("sell1"))

    def test_hanging_process_tick_cancels_far_order(self):
        c, conn = _make_controller(hanging_orders_enabled=True, hanging_orders_cancel_pct=Decimal("1"))
        # A live hanging order priced far from mid (3.5) -> cancelled.
        conn.in_flight_orders["h1"] = _FakeInFlightOrder("h1", "XSGD-MYRT", TradeType.SELL, Decimal("5.0"), Decimal("1"))
        c._active_orders["h1"] = "XSGD-MYRT"
        c._hanging_order_ids.add("h1")
        c._hanging_process_tick()
        self.assertIn(("XSGD-MYRT", "h1"), conn.cancelled)
        self.assertNotIn("h1", c._hanging_order_ids)

    # --------------------------------------------------------- reconcile/accounting
    def test_reconcile_detects_fill_and_updates_accounting(self):
        c, conn = _make_controller()
        ifo = _FakeInFlightOrder("o1", "XSGD-MYRT", TradeType.BUY, Decimal("3.48"), Decimal("100"))
        ifo.order_fills["trade-1"] = _FakeFill("trade-1", Decimal("40"), Decimal("139.2"))
        conn.in_flight_orders["o1"] = ifo
        c._active_orders["o1"] = "XSGD-MYRT"
        c._reconcile_and_account()
        self.assertEqual(c._filled_base_balance, Decimal("40"))         # buy adds base
        self.assertEqual(c._accounting["fills"], Decimal("1"))
        self.assertEqual(c._accounting["volume_quote"], Decimal("139.2"))

    def test_reconcile_completion_increments_ping_pong_counter(self):
        c, conn = _make_controller()
        ifo = _FakeInFlightOrder("o1", "XSGD-MYRT", TradeType.SELL, Decimal("3.52"), Decimal("100"),
                                 is_filled=True, is_done=True)
        conn.in_flight_orders["o1"] = ifo
        c._active_orders["o1"] = "XSGD-MYRT"
        c._reconcile_and_account()
        self.assertEqual(c._filled_sells_balance, 1)
        self.assertNotIn("o1", c._active_orders)  # done order pruned

    def test_reconcile_clears_in_flight_cancel_when_gone(self):
        c, conn = _make_controller()
        c._in_flight_cancels.add("gone")
        c._reconcile_and_account()
        self.assertNotIn("gone", c._in_flight_cancels)

    # ------------------------------------------------------------ to_create gate
    def test_should_wait_blocks_create_while_cancel_in_flight(self):
        self.controller._in_flight_cancels.add("c1")
        proposal = self.controller._create_base_proposal()
        self.assertFalse(self.controller._to_create_orders(proposal))

    def test_to_create_orders_true_when_clear(self):
        proposal = self.controller._create_base_proposal()
        self.assertTrue(self.controller._to_create_orders(proposal))

    # --------------------------------------------------------- end-to-end dry run
    def test_determine_executor_actions_places_vl_batch(self):
        actions = self.controller.determine_executor_actions()
        self.assertEqual(actions, [])                      # no executor actions emitted
        self.assertEqual(len(self.connector.created_batches), 1)
        self.assertEqual(len(self.connector.created_batches[0]), 18)
        self.assertEqual(len(self.controller._active_orders), 18)

    def test_determine_executor_actions_noop_when_not_ready(self):
        self.connector.ready = False
        actions = self.controller.determine_executor_actions()
        self.assertEqual(actions, [])
        self.assertEqual(len(self.connector.created_batches), 0)

    def test_update_processed_data_populates_telemetry(self):
        asyncio.run(self.controller.update_processed_data())
        self.assertIn("reference_price", self.controller.processed_data)
        self.assertEqual(self.controller.processed_data["reference_price"], Decimal("3.5"))
        info = self.controller.get_custom_info()
        self.assertEqual(info["vl_triangular_enabled"], True)


if __name__ == "__main__":
    unittest.main()

import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import dateutil.parser as dp
from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.sera import sera_constants as CONSTANTS, sera_utils, sera_web_utils as web_utils
from hummingbot.connector.exchange.sera.sera_api_order_book_data_source import SeraAPIOrderBookDataSource
from hummingbot.connector.exchange.sera.sera_api_user_stream_data_source import SeraAPIUserStreamDataSource
from hummingbot.connector.exchange.sera.sera_auth import SeraAuth
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair, split_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future, safe_gather
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class SeraExchange(ExchangePyBase):
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0

    web_utils = web_utils

    def __init__(
            self,
            sera_api_key: str,
            sera_api_secret: str,
            sera_wallet_address: str,
            sera_private_key: str,
            balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
            rate_limits_share_pct: Decimal = Decimal("100"),
            trading_pairs: Optional[List[str]] = None,
            trading_required: bool = True,
            domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):
        self.api_key = sera_api_key
        self.api_secret = sera_api_secret
        self.wallet_address = sera_wallet_address.lower() if sera_wallet_address else None
        self.private_key = sera_private_key
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs or []
        self._market_info: Dict[str, Dict[str, Any]] = {}
        self._token_info_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._order_uuid_ints: Dict[str, str] = {}
        self._executor_id: Optional[int] = None
        self._eip712_domain: Optional[Dict[str, Any]] = None
        super().__init__(balance_asset_limit, rate_limits_share_pct)

    @property
    def authenticator(self) -> SeraAuth:
        return SeraAuth(
            api_key=self.api_key,
            api_secret=self.api_secret,
            wallet_address=self.wallet_address,
            private_key=self.private_key,
        )

    @property
    def name(self) -> str:
        return "sera"

    @property
    def rate_limits_rules(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def client_order_id_max_length(self) -> int:
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self) -> str:
        return CONSTANTS.HBOT_ORDER_ID_PREFIX

    @property
    def trading_rules_request_path(self) -> str:
        return CONSTANTS.MARKETS_PATH_URL

    @property
    def trading_pairs_request_path(self) -> str:
        return CONSTANTS.MARKETS_PATH_URL

    @property
    def check_network_request_path(self) -> str:
        return CONSTANTS.HEALTH_PATH_URL

    @property
    def trading_pairs(self) -> List[str]:
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return False

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    @property
    def status_dict(self) -> Dict[str, bool]:
        status = super().status_dict
        status["user_stream_initialized"] = True
        return status

    def supported_order_types(self):
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER]

    def buy(
            self,
            trading_pair: str,
            amount: Decimal,
            order_type=OrderType.LIMIT,
            price: Decimal = s_decimal_NaN,
            **kwargs,
    ) -> str:
        order_id = str(uuid.uuid4())
        safe_ensure_future(self._create_order(
            trade_type=TradeType.BUY,
            order_id=order_id,
            trading_pair=trading_pair,
            amount=amount,
            order_type=order_type,
            price=price,
            **kwargs,
        ))
        return order_id

    def sell(
            self,
            trading_pair: str,
            amount: Decimal,
            order_type: OrderType = OrderType.LIMIT,
            price: Decimal = s_decimal_NaN,
            **kwargs,
    ) -> str:
        order_id = str(uuid.uuid4())
        safe_ensure_future(self._create_order(
            trade_type=TradeType.SELL,
            order_id=order_id,
            trading_pair=trading_pair,
            amount=amount,
            order_type=order_type,
            price=price,
            **kwargs,
        ))
        return order_id

    def _get_fee(
            self,
            base_currency: str,
            quote_currency: str,
            order_type: OrderType,
            order_side: TradeType,
            amount: Decimal,
            price: Decimal = s_decimal_NaN,
            is_maker: Optional[bool] = None,
    ) -> TradeFeeBase:
        is_maker = order_type is OrderType.LIMIT_MAKER if is_maker is None else is_maker
        return build_trade_fee(
            exchange=self.name,
            is_maker=is_maker,
            order_side=order_side,
            order_type=order_type,
            amount=amount,
            price=price,
            base_currency=base_currency.upper(),
            quote_currency=quote_currency.upper(),
        )

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception) -> bool:
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return "404" in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return "404" in str(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
            auth=self._auth,
        )

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return SeraAPIOrderBookDataSource(trading_pairs=self._trading_pairs, connector=self)

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return SeraAPIUserStreamDataSource()

    async def _make_network_check_request(self):
        await self._api_get(path_url=self.check_network_request_path, limit_id=CONSTANTS.HEALTH_PATH_URL)

    async def _make_trading_rules_request(self) -> Any:
        return await self._api_get(path_url=self.trading_rules_request_path, limit_id=CONSTANTS.MARKETS_PATH_URL)

    async def _make_trading_pairs_request(self) -> Any:
        return await self._api_get(path_url=self.trading_pairs_request_path, limit_id=CONSTANTS.MARKETS_PATH_URL)

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        rules = []
        markets = exchange_info_dict.get("markets", exchange_info_dict)
        for market in filter(sera_utils.is_exchange_information_valid, markets):
            try:
                trading_pair = self._trading_pair_from_market(market=market)
                amount_step = Decimal(str(market["amount_step"]))
                price_step = Decimal(str(market["price_step"]))
                min_order_size = Decimal(str(market.get("min_ask_amount") or "0"))
                if min_order_size == Decimal("0"):
                    min_order_size = amount_step
                rules.append(TradingRule(
                    trading_pair=trading_pair,
                    min_order_size=min_order_size,
                    min_price_increment=price_step,
                    min_base_amount_increment=amount_step,
                    min_notional_size=Decimal(str(market.get("min_bid_quote_amount") or "0")),
                ))
                self._market_info[trading_pair] = market
            except Exception:
                self.logger().exception(f"Error parsing Sera trading rule {market}. Skipping.")
        return rules

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        markets = exchange_info.get("markets", exchange_info)
        for market in filter(sera_utils.is_exchange_information_valid, markets):
            mapping[market["symbol"]] = self._trading_pair_from_market(market=market)
        self._set_trading_pair_symbol_map(mapping)

    async def _place_order(
            self,
            order_id: str,
            trading_pair: str,
            amount: Decimal,
            trade_type: TradeType,
            order_type: OrderType,
            price: Decimal,
            **kwargs,
    ) -> Tuple[str, float]:
        if order_type not in [OrderType.LIMIT, OrderType.LIMIT_MAKER]:
            raise ValueError(f"Sera supports limit orders only. Unsupported order type: {order_type}")
        await self._ensure_exchange_config()
        market = self._market_info[trading_pair]
        side = CONSTANTS.SIDE_BID if trade_type is TradeType.BUY else CONSTANTS.SIDE_ASK
        expiration = kwargs.get("expiration") or await self._new_expiration_timestamp()
        uuid_int = self._encode_standalone_uuid(order_id=order_id, executor_id=self._executor_id)
        self._order_uuid_ints[order_id] = uuid_int
        preview_payload = {
            "owner_address": self.wallet_address,
            "side": side,
            "amount": f"{amount:f}",
            "price": f"{price:f}",
            "order_type": CONSTANTS.ORDER_TYPE_LIMIT,
            "from_address": market["base_address"],
            "to_address": market["quote_address"],
            "order_id": order_id,
            "uuid_int": uuid_int,
            "expiration": expiration,
        }
        preview = await self._api_post(
            path_url=CONSTANTS.PREVIEW_ORDER_PATH_URL,
            data=preview_payload,
            is_auth_required=False,
            limit_id=CONSTANTS.PREVIEW_ORDER_PATH_URL,
        )
        eip712_order = preview["eip712_order"]
        signature = self.authenticator.sign_typed_data(
            domain=self._eip712_domain,
            message_types=preview.get("eip712_types") or CONSTANTS.ORDER_TYPES,
            message=eip712_order,
        )
        order_payload = {
            **preview_payload,
            "amount": preview.get("normalized_amount", preview_payload["amount"]),
            "price": preview.get("normalized_price", preview_payload["price"]),
            "signature": signature,
        }
        order_result = await self._api_post(
            path_url=CONSTANTS.ORDERS_PATH_URL,
            data=order_payload,
            is_auth_required=False,
            limit_id=CONSTANTS.ORDERS_PATH_URL,
        )
        return str(order_result["order_id"]), self.current_timestamp

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        await self._ensure_exchange_config()
        exchange_order_id = await tracked_order.get_exchange_order_id()
        uuid_int = self._order_uuid_ints.get(exchange_order_id) or self._order_uuid_ints.get(order_id)
        if uuid_int is None:
            order_status = await self._api_get(
                path_url=CONSTANTS.ORDER_PATH_URL.format(order_id=exchange_order_id),
                is_auth_required=True,
                limit_id=CONSTANTS.ORDER_PATH_URL,
            )
            uuid_int = str(order_status["uuid_int"])
            self._order_uuid_ints[exchange_order_id] = uuid_int
        signature = self.authenticator.sign_typed_data(
            domain=self._eip712_domain,
            message_types=CONSTANTS.CANCEL_ORDER_TYPES,
            message={"owner": self.wallet_address, "orderId": int(uuid_int)},
        )
        await self._api_post(
            path_url=CONSTANTS.CANCEL_ORDER_PATH_URL,
            data={
                "owner_address": self.wallet_address,
                "order_id": exchange_order_id,
                "uuid_int": uuid_int,
                "signature": signature,
            },
            is_auth_required=False,
            limit_id=CONSTANTS.CANCEL_ORDER_PATH_URL,
        )
        return True

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        exchange_order_id = await tracked_order.get_exchange_order_id()
        order_data = await self._api_get(
            path_url=CONSTANTS.ORDER_PATH_URL.format(order_id=exchange_order_id),
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_PATH_URL,
        )
        if order_data.get("uuid_int"):
            self._order_uuid_ints[exchange_order_id] = str(order_data["uuid_int"])
        return OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self._timestamp_from_order(order_data),
            new_state=self._order_state_from_order_data(order_data=order_data),
        )

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        if order.exchange_order_id is None:
            return []
        fills_response = await self._api_get(
            path_url=CONSTANTS.FILLS_PATH_URL.format(order_id=order.exchange_order_id),
            params={"limit": 500, "offset": 0},
            is_auth_required=True,
            limit_id=CONSTANTS.FILLS_PATH_URL,
        )
        trade_updates = []
        for fill in fills_response.get("items", []):
            if order.exchange_order_id not in [fill.get("maker_order_id"), fill.get("taker_order_id")]:
                continue
            price = Decimal(str(fill["price"]))
            quantity = Decimal(str(fill["quantity"]))
            fee = TradeFeeBase.new_spot_fee(
                fee_schema=self.trade_fee_schema(),
                trade_type=order.trade_type,
                flat_fees=self._flat_fees_from_fill(fill=fill),
            )
            trade_updates.append(TradeUpdate(
                trade_id=self._trade_id_from_fill(fill=fill),
                client_order_id=order.client_order_id,
                exchange_order_id=order.exchange_order_id,
                trading_pair=order.trading_pair,
                fee=fee,
                fill_base_amount=quantity,
                fill_quote_amount=quantity * price,
                fill_price=price,
                fill_timestamp=self._timestamp_from_fill(fill=fill),
            ))
        return trade_updates

    async def _update_balances(self):
        balances_response = await self._api_get(
            path_url=CONSTANTS.BALANCES_PATH_URL,
            params={"owner_address": self.wallet_address},
            is_auth_required=True,
            limit_id=CONSTANTS.BALANCES_PATH_URL,
        )
        remote_assets = set()
        for balance in balances_response.get("balances", []):
            symbol = balance["symbol"].upper()
            decimals = Decimal(f"1e{int(balance['decimals'])}")
            total = Decimal(str(balance["total"])) / decimals
            available = Decimal(str(balance["vault_available"])) / decimals
            self._account_balances[symbol] = total
            self._account_available_balances[symbol] = available
            remote_assets.add(symbol)
        for asset in set(self._account_balances.keys()) - remote_assets:
            del self._account_balances[asset]
            self._account_available_balances.pop(asset, None)

    async def _update_trading_fees(self):
        return None

    async def _user_stream_event_listener(self):
        while True:
            await self._sleep(60.0)

    async def _status_polling_loop_fetch_updates(self):
        await safe_gather(
            self._update_balances(),
            self._update_orders_fills(orders=list(self._order_tracker.all_fillable_orders.values())),
            self._update_orders(),
        )

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        await self._ensure_exchange_config()
        base, quote = split_hb_trading_pair(trading_pair=trading_pair)
        params = {"base": base, "quote": quote}
        token_base = self._token_info_by_symbol.get(base)
        token_quote = self._token_info_by_symbol.get(quote)
        if token_base is not None and token_quote is not None:
            params = {"base": token_base.get("currency", base), "quote": token_quote.get("currency", quote)}
        rate_response = await self._api_get(
            path_url=CONSTANTS.FX_RATE_PATH_URL,
            params=params,
            is_auth_required=False,
            limit_id=CONSTANTS.FX_RATE_PATH_URL,
        )
        return float(rate_response["rate"])

    async def _ensure_exchange_config(self):
        if self._executor_id is None:
            health = await self._api_get(
                path_url=CONSTANTS.HEALTH_PATH_URL,
                is_auth_required=False,
                limit_id=CONSTANTS.HEALTH_PATH_URL,
            )
            self._executor_id = int(health["executor_id"])
        if self._eip712_domain is None:
            config = await self._api_get(
                path_url=CONSTANTS.CONFIG_PATH_URL,
                is_auth_required=False,
                limit_id=CONSTANTS.CONFIG_PATH_URL,
            )
            self._eip712_domain = config["eip712_domain"]
        if len(self._token_info_by_symbol) == 0:
            tokens = await self._api_get(
                path_url=CONSTANTS.TOKENS_PATH_URL,
                is_auth_required=False,
                limit_id=CONSTANTS.TOKENS_PATH_URL,
            )
            self._token_info_by_symbol = {
                token["symbol"].upper(): token
                for token in tokens.get("tokens", [])
            }

    async def _new_expiration_timestamp(self) -> int:
        time_response = await self._api_get(
            path_url=CONSTANTS.TIME_PATH_URL,
            is_auth_required=False,
            limit_id=CONSTANTS.TIME_PATH_URL,
        )
        return int(time_response["timestamp"]) + CONSTANTS.ORDER_EXPIRATION_SECONDS

    @staticmethod
    def _encode_standalone_uuid(order_id: str, executor_id: int) -> str:
        raw = int(uuid.UUID(order_id))
        group = raw >> 16
        return str((executor_id << 252) | (raw << 124) | (group << 12))

    @staticmethod
    def _trading_pair_from_market(market: Dict[str, Any]) -> str:
        return combine_to_hb_trading_pair(base=market["base_symbol"].upper(), quote=market["quote_symbol"].upper())

    @staticmethod
    def _timestamp_from_order(order_data: Dict[str, Any]) -> float:
        timestamp = order_data.get("updated_at") or order_data.get("created_at")
        return dp.parse(timestamp).timestamp() if timestamp else time.time()

    @staticmethod
    def _timestamp_from_fill(fill: Dict[str, Any]) -> float:
        timestamp = fill.get("timestamp")
        return dp.parse(timestamp).timestamp() if isinstance(timestamp, str) else float(timestamp or time.time())

    @staticmethod
    def _trade_id_from_fill(fill: Dict[str, Any]) -> str:
        return str(fill.get("tx_hash") or (
            f"{fill.get('maker_order_id')}-{fill.get('taker_order_id')}-"
            f"{fill.get('timestamp')}-{fill.get('quantity')}-{fill.get('price')}"
        ))

    @staticmethod
    def _flat_fees_from_fill(fill: Dict[str, Any]) -> List[TokenAmount]:
        fees = []
        economics = fill.get("settlement_economics") or {}
        for fee in economics.get("fees_paid") or []:
            token = fee.get("token")
            amount = fee.get("amount")
            if token is not None and amount is not None:
                fees.append(TokenAmount(token=token.upper(), amount=Decimal(str(amount))))
        return fees

    @staticmethod
    def _order_state_from_order_data(order_data: Dict[str, Any]) -> OrderState:
        status = order_data["status"]
        state = CONSTANTS.ORDER_STATE.get(status, OrderState.FAILED)
        if state is OrderState.OPEN and Decimal(str(order_data.get("filled_base_amount") or "0")) > Decimal("0"):
            state = OrderState.PARTIALLY_FILLED
        return state

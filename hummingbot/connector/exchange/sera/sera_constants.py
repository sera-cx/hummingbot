from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

DEFAULT_DOMAIN = "sera"

HBOT_ORDER_ID_PREFIX = ""
MAX_ORDER_ID_LEN = 36

REST_URL = "https://api.testnet.sera.cx/api/v1"

HEALTH_PATH_URL = "/health"
TIME_PATH_URL = "/system/time"
TOKENS_PATH_URL = "/tokens"
MARKETS_PATH_URL = "/markets"
CONFIG_PATH_URL = "/config"
FX_RATE_PATH_URL = "/fx/rate"

PREVIEW_ORDER_PATH_URL = "/orders/preview"
ORDERS_PATH_URL = "/orders"
CANCEL_ORDER_PATH_URL = "/orders/cancel"
ORDER_PATH_URL = "/orders/{order_id}"
FILLS_PATH_URL = "/fills/{order_id}"
BALANCES_PATH_URL = "/balances"

SIDE_BID = "bid"
SIDE_ASK = "ask"
ORDER_TYPE_LIMIT = "limit"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ORDER_EXPIRATION_SECONDS = 24 * 60 * 60

ORDER_STATE = {
    "pending": OrderState.OPEN,
    "matched": OrderState.FILLED,
    "settled": OrderState.FILLED,
    "cancelled": OrderState.CANCELED,
    "failed": OrderState.FAILED,
}

ORDER_TYPES = {
    "Order": [
        {"name": "user", "type": "address"},
        {"name": "expiration", "type": "uint48"},
        {"name": "feeBps", "type": "uint48"},
        {"name": "recipient", "type": "address"},
        {"name": "fromToken", "type": "address"},
        {"name": "toToken", "type": "address"},
        {"name": "fromAmount", "type": "uint256"},
        {"name": "toAmount", "type": "uint256"},
        {"name": "initialDepositAmount", "type": "uint256"},
        {"name": "uuid", "type": "uint256"},
    ]
}

CANCEL_ORDER_TYPES = {
    "CancelOrder": [
        {"name": "owner", "type": "address"},
        {"name": "orderId", "type": "uint256"},
    ]
}

ONE_SECOND = 1
ONE_MINUTE = 60
MAX_REQUESTS_PER_MINUTE = 600
READ_REQUESTS_PER_SECOND = 10

PUBLIC_REQUEST_WEIGHT = "PUBLIC_REQUEST_WEIGHT"
READ_REQUEST_WEIGHT = "READ_REQUEST_WEIGHT"
TRADING_REQUEST_WEIGHT = "TRADING_REQUEST_WEIGHT"

RATE_LIMITS = [
    RateLimit(limit_id=PUBLIC_REQUEST_WEIGHT, limit=MAX_REQUESTS_PER_MINUTE, time_interval=ONE_MINUTE),
    RateLimit(limit_id=READ_REQUEST_WEIGHT, limit=READ_REQUESTS_PER_SECOND, time_interval=ONE_SECOND),
    RateLimit(limit_id=TRADING_REQUEST_WEIGHT, limit=READ_REQUESTS_PER_SECOND, time_interval=ONE_SECOND),
    RateLimit(
        limit_id=HEALTH_PATH_URL,
        limit=MAX_REQUESTS_PER_MINUTE,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(PUBLIC_REQUEST_WEIGHT, 1)],
    ),
    RateLimit(
        limit_id=TIME_PATH_URL,
        limit=MAX_REQUESTS_PER_MINUTE,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(PUBLIC_REQUEST_WEIGHT, 1)],
    ),
    RateLimit(
        limit_id=MARKETS_PATH_URL,
        limit=MAX_REQUESTS_PER_MINUTE,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(PUBLIC_REQUEST_WEIGHT, 1)],
    ),
    RateLimit(
        limit_id=TOKENS_PATH_URL,
        limit=MAX_REQUESTS_PER_MINUTE,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(PUBLIC_REQUEST_WEIGHT, 1)],
    ),
    RateLimit(
        limit_id=CONFIG_PATH_URL,
        limit=MAX_REQUESTS_PER_MINUTE,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(PUBLIC_REQUEST_WEIGHT, 1)],
    ),
    RateLimit(
        limit_id=FX_RATE_PATH_URL,
        limit=MAX_REQUESTS_PER_MINUTE,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(PUBLIC_REQUEST_WEIGHT, 1)],
    ),
    RateLimit(
        limit_id=PREVIEW_ORDER_PATH_URL,
        limit=READ_REQUESTS_PER_SECOND,
        time_interval=ONE_SECOND,
        linked_limits=[LinkedLimitWeightPair(TRADING_REQUEST_WEIGHT, 1)],
    ),
    RateLimit(
        limit_id=ORDERS_PATH_URL,
        limit=READ_REQUESTS_PER_SECOND,
        time_interval=ONE_SECOND,
        linked_limits=[LinkedLimitWeightPair(TRADING_REQUEST_WEIGHT, 1)],
    ),
    RateLimit(
        limit_id=CANCEL_ORDER_PATH_URL,
        limit=READ_REQUESTS_PER_SECOND,
        time_interval=ONE_SECOND,
        linked_limits=[LinkedLimitWeightPair(TRADING_REQUEST_WEIGHT, 1)],
    ),
    RateLimit(
        limit_id=BALANCES_PATH_URL,
        limit=READ_REQUESTS_PER_SECOND,
        time_interval=ONE_SECOND,
        linked_limits=[LinkedLimitWeightPair(READ_REQUEST_WEIGHT, 1)],
    ),
    RateLimit(
        limit_id=FILLS_PATH_URL,
        limit=READ_REQUESTS_PER_SECOND,
        time_interval=ONE_SECOND,
        linked_limits=[LinkedLimitWeightPair(READ_REQUEST_WEIGHT, 1)],
    ),
]

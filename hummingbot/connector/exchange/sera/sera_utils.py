from decimal import Decimal
from typing import Any, Dict

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

CENTRALIZED = True
EXAMPLE_PAIR = "EURC-USDC"

DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0"),
    taker_percent_fee_decimal=Decimal("0"),
    buy_percent_fee_deducted_from_returns=True,
)


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    return bool(exchange_info.get("symbol") and exchange_info.get("base_address") and exchange_info.get("quote_address"))


class SeraConfigMap(BaseConnectorConfigMap):
    connector: str = "sera"
    sera_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your Sera API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    sera_api_secret: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your Sera API secret",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    sera_wallet_address: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your Sera wallet address",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    sera_wallet_private_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your Sera wallet private key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    model_config = ConfigDict(title="sera")


KEYS = SeraConfigMap.model_construct()

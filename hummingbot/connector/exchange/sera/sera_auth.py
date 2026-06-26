from typing import Any, Dict

from eth_account import Account
from eth_account.messages import encode_typed_data

from hummingbot.connector.utils import to_0x_hex
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


class SeraAuth(AuthBase):
    def __init__(
            self,
            api_key: str,
            api_secret: str,
            wallet_address: str,
            private_key: str,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.wallet_address = wallet_address.lower() if wallet_address else None
        self.wallet = Account.from_key(private_key) if private_key else None

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        headers = dict(request.headers or {})
        headers["Authorization"] = f"Bearer {self.api_key}:{self.api_secret}"
        request.headers = headers
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        return request

    def sign_typed_data(self, domain: Dict[str, Any], message_types: Dict[str, Any], message: Dict[str, Any]) -> str:
        message_types = self._message_types_for_signing(message_types=message_types)
        signable = encode_typed_data(
            domain_data=domain,
            message_types=message_types,
            message_data=self._coerce_typed_data_message(message_types=message_types, message=message),
        )
        return to_0x_hex(self.wallet.sign_message(signable_message=signable).signature)

    @classmethod
    def _coerce_typed_data_message(cls, message_types: Dict[str, Any], message: Dict[str, Any]) -> Dict[str, Any]:
        primary_type = next(iter(message_types))
        return cls._coerce_struct(message_types=message_types, struct_name=primary_type, message=message)

    @staticmethod
    def _message_types_for_signing(message_types: Dict[str, Any]) -> Dict[str, Any]:
        return {
            type_name: fields
            for type_name, fields in message_types.items()
            if type_name != "EIP712Domain"
        }

    @classmethod
    def _coerce_struct(cls, message_types: Dict[str, Any], struct_name: str, message: Dict[str, Any]) -> Dict[str, Any]:
        coerced = dict(message)
        for field in message_types.get(struct_name, []):
            name = field["name"]
            field_type = field["type"]
            if name not in coerced or coerced[name] is None:
                continue
            coerced[name] = cls._coerce_value(
                message_types=message_types,
                field_type=field_type,
                value=coerced[name],
            )
        return coerced

    @classmethod
    def _coerce_value(cls, message_types: Dict[str, Any], field_type: str, value: Any) -> Any:
        if field_type.endswith("[]") or field_type not in message_types:
            if field_type.startswith(("uint", "int")) and isinstance(value, str):
                return int(value)
            return value
        if isinstance(value, dict):
            return cls._coerce_struct(message_types=message_types, struct_name=field_type, message=value)
        return value

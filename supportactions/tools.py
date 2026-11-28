from dataclasses import dataclass
from decimal import Decimal
import math
from typing import Callable

from .backend import Backend
from .trace import INVALID_VALUE


class Rejected(Exception):
    pass


def money_cents(value) -> int:
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise Rejected("参数类型错误")
    amount = Decimal(str(value))
    if amount <= 0 or amount.as_tuple().exponent < -2:
        raise Rejected("退款金额须为大于零且最多两位小数的金额")
    numerator, denominator = amount.as_integer_ratio()
    return numerator * 100 // denominator


def lookup_order(backend: Backend, order_id: str) -> dict:
    row = backend.order(order_id)
    result = {"order_id": order_id, "paid_cents": row["paid_cents"], "shipped": bool(row["shipped"])}
    if row["note"]:
        result["note"] = row["note"]
    return result


def refund(backend: Backend, order_id: str, amount) -> dict:
    return backend.refund(order_id, money_cents(amount))


@dataclass(frozen=True)
class Tool:
    risk: str
    params: dict[str, type | tuple[type, ...]]
    handler: Callable


TOOLS = {
    "lookup_order": Tool("read", {"order_id": str}, lookup_order),
    "list_orders": Tool("read", {"account": str}, Backend.list_orders),
    "change_address": Tool("low", {"order_id": str, "new_address": str}, Backend.change_address),
    "refund": Tool("high", {"order_id": str, "amount": (int, float)}, refund),
}


def validate(backend: Backend, account: str, name: str, params: dict) -> None:
    schema = TOOLS[name].params
    if not isinstance(params, dict):
        raise Rejected("工具参数必须为对象")
    if params.keys() != schema.keys():
        raise Rejected("工具参数缺失或包含未定义字段")
    for key, types in schema.items():
        allowed = types if isinstance(types, tuple) else (types,)
        value = params[key]
        if type(value) not in allowed or (isinstance(value, str) and not value.strip()):
            raise Rejected("参数类型错误或值为空")

    if name == "list_orders":
        if params["account"] != account:
            raise Rejected("无权查看其他账户的订单")
        return

    if name == "lookup_order" and params["order_id"] == INVALID_VALUE:
        raise Rejected("订单号格式错误")
    order = backend.order(params["order_id"])
    if order is None:
        raise Rejected("订单不存在")
    if order["account"] != account:
        raise Rejected("无权操作该订单")
    if name == "change_address" and order["shipped"]:
        raise Rejected("订单已发货")
    if name == "refund" and money_cents(params["amount"]) > order["paid_cents"]:
        raise Rejected("退款金额超过实付")

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Call:
    tool: str
    params: dict
    depends_on: tuple[int, ...] = ()


def plan(message: str, account: str) -> list[Call]:
    if message == "坏计划":
        return [Call("list_orders", {"account": account}), Call("delete_order", {})]

    order = re.search(r"[A-Z]-[0-9]+", message)
    params = {"order_id": order.group()} if order else {}
    if "退款" in message:
        amount = re.search(r"退款\s*(-?[0-9]+(?:\.[0-9]{1,2})?)\s*元", message)
        if amount:
            params["amount"] = float(amount.group(1))
        return [Call("refund", params)]
    if "改地址" in message:
        address = message.partition("改地址")[2].strip()
        if address:
            params["new_address"] = address
        return [Call("change_address", params)]
    if order or "查订单" in message:
        return [Call("lookup_order", params)]
    if "我的订单" in message:
        return [Call("list_orders", {"account": account})]
    return [Call("无法识别", {})]

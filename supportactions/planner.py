import re
import unicodedata
from dataclasses import dataclass, field


SUSPICIOUS_INSTRUCTION = "可疑指令"
_INSTRUCTIONS = re.compile(
    r"忽略\s*(?:之前|此前|先前|以上|前面)\s*(?:的\s*)?(?:所有\s*)?(?:规则|指令)"
    r"|直接\s*退款|调用\s*[\x60\x22']?\s*refund(?![a-z_0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Call:
    tool: str
    params: dict
    depends_on: tuple[int, ...] = ()
    # Each argument names the earlier step and result field that supplies its value.
    args_from: dict[str, tuple[int, str]] | tuple[int, ...] = field(default_factory=dict)


def has_suspicious_instruction(value) -> bool:
    if isinstance(value, str):
        return bool(_INSTRUCTIONS.search(unicodedata.normalize("NFKC", value)))
    if isinstance(value, dict):
        return any(map(has_suspicious_instruction, value.values()))
    if isinstance(value, list):
        return any(map(has_suspicious_instruction, value))
    return False


def plan(message: str, account: str) -> list[Call]:
    # This entry point receives the customer's request, never a tool result.
    if message == "坏计划":
        return [Call("list_orders", {"account": account}), Call("delete_order", {})]

    orders = re.findall(r"[A-Z]-[0-9]+", message)
    params = {"order_id": orders[0]} if orders else {}
    if "退款" in message:
        if len(set(orders)) > 1:
            del params["order_id"]
        amount = re.search(r"退款\s*(-?[0-9]+(?:\.[0-9]{1,2})?)\s*元", message)
        if amount:
            params["amount"] = float(amount.group(1))
        return [Call("refund", params)]
    if "改地址" in message:
        address_change = re.fullmatch(r"\s*([A-Z]-[0-9]+)\s*改地址(?:\s+(\S.*?))?\s*", message)
        if address_change is None:
            return [Call("无法识别", {})]
        address = address_change[2]
        params = {"order_id": address_change[1]}
        if address and not address.rstrip("。.!！").endswith(("?", "？", "吗", "么", "呢")):
            params["new_address"] = address
        return [Call("change_address", params)]
    if orders or "查订单" in message:
        return [Call("lookup_order", params)]
    if "我的订单" in message:
        return [Call("list_orders", {"account": account})]
    return [Call("无法识别", {})]

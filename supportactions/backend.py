import json
from pathlib import Path
import sqlite3

from .trace import safe_address


class BackendError(Exception):
    pass


class Backend:
    """The runner holds a write transaction around each workflow transition."""

    def __init__(self, db: sqlite3.Connection, faults: dict[str, str] | None = None):
        self.db = db
        self.faults = faults or {}

    def initialize(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY, account TEXT, paid_cents INTEGER,
                shipped INTEGER, address TEXT, note TEXT
            )
        """)
        if not any(row["name"] == "note" for row in self.db.execute("PRAGMA table_info(orders)")):
            self.db.execute("ALTER TABLE orders ADD COLUMN note TEXT")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS effects (
                effect_key TEXT PRIMARY KEY, result TEXT
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS refunds (order_id TEXT, amount_cents INTEGER)
        """)
        fixture = Path(__file__).resolve().parent.parent / "data" / "orders.json"
        orders = json.loads(fixture.read_text(encoding="utf-8"))
        self.db.executemany(
            """INSERT OR IGNORE INTO orders (order_id, account, paid_cents, shipped, address, note)
               VALUES (:order_id, :account, :paid_cents, :shipped, :address, :note)""",
            ({"note": None, **order} for order in orders),
        )

    def order(self, order_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()

    def list_orders(self, account: str) -> dict:
        rows = self.db.execute(
            "SELECT order_id, paid_cents, shipped FROM orders WHERE account = ? ORDER BY order_id",
            (account,),
        )
        return {"orders": [
            {"order_id": row["order_id"], "paid_cents": row["paid_cents"], "shipped": bool(row["shipped"])}
            for row in rows
        ]}

    def result(self, key: str) -> dict | None:
        row = self.db.execute("SELECT result FROM effects WHERE effect_key = ?", (key,)).fetchone()
        return json.loads(row["result"]) if row else None

    def execute(self, key: str, handler, **params) -> dict:
        existing = self.result(key)
        if existing is not None:
            return existing

        fault = self.faults.get(key)
        if fault == "unknown":
            raise TimeoutError
        if fault == "failure":
            raise BackendError("业务系统执行失败")

        result = handler(**params)
        self.db.execute("INSERT OR REPLACE INTO effects VALUES (?, ?)", (key, json.dumps(result)))
        if fault == "timeout_after_success":
            raise TimeoutError
        return result

    def change_address(self, order_id: str, new_address: str) -> dict:
        self.db.execute("UPDATE orders SET address = ? WHERE order_id = ?", (safe_address(new_address), order_id))
        return {"order_id": order_id, "address_changed": True}

    def refund(self, order_id: str, amount_cents: int) -> dict:
        inserted = self.db.execute("""
            INSERT INTO refunds (order_id, amount_cents)
            SELECT order_id, ? FROM orders
            WHERE order_id = ? AND ? <= paid_cents - (
                SELECT COALESCE(SUM(amount_cents), 0) FROM refunds WHERE refunds.order_id = orders.order_id
            )
        """, (amount_cents, order_id, amount_cents))
        if inserted.rowcount == 0:
            raise BackendError("退款金额超过剩余可退金额")
        return {"order_id": order_id, "refunded_cents": amount_cents}

    def refund_count(self, order_id: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM refunds WHERE order_id = ?", (order_id,),
        ).fetchone()[0]

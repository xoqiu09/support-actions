from datetime import datetime, timedelta
import sqlite3


PENDING = "待审"
APPROVED = "已批准"
REJECTED = "已拒绝"
EXPIRED = "已过期"
TTL = timedelta(minutes=5)


class Approvals:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def initialize(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS approvals (
                run_id TEXT, step INTEGER, status TEXT, deadline TEXT,
                PRIMARY KEY (run_id, step)
            )
        """)

    def enqueue(self, run_id: str, step: int, now: datetime) -> None:
        self.db.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?)",
            (run_id, step, PENDING, (now + TTL).isoformat()),
        )

    def get(self, run_id: str, step: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM approvals WHERE run_id = ? AND step = ?", (run_id, step),
        ).fetchone()

    def all(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM approvals ORDER BY run_id, step").fetchall()

    def decide(self, run_id: str, step: int, now: datetime, decision: str = PENDING) -> str:
        row = self.get(run_id, step)
        if row is None:
            raise ValueError("该步骤没有审批记录")
        if row["status"] != PENDING:
            return row["status"]
        status = EXPIRED if now >= datetime.fromisoformat(row["deadline"]) else decision
        if status != PENDING:
            self.db.execute(
                "UPDATE approvals SET status = ? WHERE run_id = ? AND step = ?",
                (status, run_id, step),
            )
        return status

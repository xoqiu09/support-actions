from datetime import datetime, timedelta
from hashlib import sha256
import sqlite3


PENDING = "待审"
APPROVED = "已批准"
REJECTED = "已拒绝"
EXPIRED = "已过期"
TTL = timedelta(minutes=5)


def _approver_digest(approver: str) -> bytes:
    return sha256(approver.encode("utf-8")).digest()


class Approvals:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def initialize(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS approvals (
                run_id TEXT, step INTEGER, status TEXT, deadline TEXT, required INTEGER,
                PRIMARY KEY (run_id, step)
            )
        """)
        if not any(row["name"] == "required" for row in self.db.execute("PRAGMA table_info(approvals)")):
            self.db.execute("ALTER TABLE approvals ADD COLUMN required INTEGER")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS approval_votes (
                run_id TEXT, step INTEGER, approver TEXT,
                PRIMARY KEY (run_id, step, approver)
            )
        """)

    def upgrade_votes(self) -> None:
        rows = self.db.execute("SELECT rowid, approver FROM approval_votes WHERE typeof(approver) = 'text'").fetchall()
        if rows:
            self.db.execute("PRAGMA secure_delete = ON")
            self.db.executemany("UPDATE approval_votes SET approver = ? WHERE rowid = ?",
                                ((_approver_digest(row["approver"]), row["rowid"]) for row in rows))

    def enqueue(self, run_id: str, step: int, now: datetime, required: int) -> None:
        self.db.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?)",
            (run_id, step, PENDING, (now + TTL).isoformat(), required),
        )

    def get(self, run_id: str, step: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM approvals WHERE run_id = ? AND step = ?", (run_id, step),
        ).fetchone()

    def all(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM approvals ORDER BY run_id, step").fetchall()

    def count_votes(self, run_id: str, step: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM approval_votes WHERE run_id = ? AND step = ?", (run_id, step),
        ).fetchone()[0]

    def decide(self, run_id: str, step: int, now: datetime, decision: str | None = None,
               *, approver: str = "default") -> str:
        if decision is None:
            decision = PENDING
        elif decision not in (APPROVED, REJECTED):
            raise ValueError("审批决定只能是“已批准”或“已拒绝”")
        row = self.get(run_id, step)
        if row is None:
            raise ValueError("该步骤没有审批记录")
        if row["status"] != PENDING:
            return row["status"]
        status = EXPIRED if now >= datetime.fromisoformat(row["deadline"]) else decision
        if status == APPROVED and row["required"] == 2:
            self.db.execute("INSERT OR IGNORE INTO approval_votes VALUES (?, ?, ?)",
                            (run_id, step, _approver_digest(approver)))
            if self.count_votes(run_id, step) < row["required"]:
                return PENDING
        if status != PENDING:
            self.db.execute(
                "UPDATE approvals SET status = ? WHERE run_id = ? AND step = ?",
                (status, run_id, step),
            )
        return status

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3


PENDING = "待审"
APPROVED = "已批准"
REJECTED = "已拒绝"
EXPIRED = "已过期"
TTL = timedelta(minutes=5)


def _approver_digest(approver: str) -> bytes:
    return sha256(approver.encode("utf-8")).digest()


def _as_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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

    def upgrade_deadlines(self) -> None:
        rows = self.db.execute("""
            SELECT approvals.run_id, approvals.step, approvals.deadline, events.payload
            FROM approvals JOIN events ON events.seq = (
                SELECT MIN(first.seq) FROM events AS first
                WHERE first.run_id = approvals.run_id AND first.step = approvals.step
            )
            WHERE approvals.status = ?
        """, (PENDING,)).fetchall()
        for row in rows:
            event = json.loads(row["payload"])
            if event["status"] != PENDING:
                continue
            enqueued_at = datetime.fromisoformat(event["time"])
            deadline = datetime.fromisoformat(row["deadline"])
            if enqueued_at.utcoffset() is None or deadline.utcoffset() is None:
                continue

            # Old deadlines use local wall time. Recover the enqueue offset from the first event.
            # Keep the stored instant if it is closer to the audited deadline, including UTC writes.
            expected = enqueued_at.astimezone(timezone.utc) + TTL
            corrected = deadline.replace(tzinfo=enqueued_at.tzinfo).astimezone(timezone.utc)
            if abs(corrected - expected) < abs(deadline - expected):
                self.db.execute("UPDATE approvals SET deadline = ? WHERE run_id = ? AND step = ?",
                                (corrected.isoformat(), row["run_id"], row["step"]))

    def enqueue(self, run_id: str, step: int, now: datetime, required: int) -> None:
        self.db.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?)",
            (run_id, step, PENDING, (_as_utc(now) + TTL).isoformat(), required),
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
        if type(step) is not int or step < 1:
            raise ValueError("步骤号须为正整数")
        if decision is None:
            decision = PENDING
        elif decision not in (APPROVED, REJECTED):
            raise ValueError("审批决定只能是“已批准”或“已拒绝”")
        row = self.get(run_id, step)
        if row is None:
            raise ValueError("该步骤没有审批记录")
        if row["status"] != PENDING:
            return row["status"]
        deadline = _as_utc(datetime.fromisoformat(row["deadline"]))
        status = EXPIRED if _as_utc(now) >= deadline else decision
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

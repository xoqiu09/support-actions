import json
import sqlite3
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from uuid import uuid4

from . import trace
from .approvals import APPROVED, EXPIRED, PENDING, REJECTED, Approvals
from .backend import Backend, BackendError
from .planner import SUSPICIOUS_INSTRUCTION, Call, has_suspicious_instruction
from .tools import TOOLS, Rejected, money_cents, validate


def _quota_windows(now: datetime) -> tuple[str, str]:
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return now.isoformat(timespec="minutes"), now.date().isoformat()


class Runner:
    def __init__(self, directory: Path, *, clock=None, faults: dict[str, str] | None = None,
                 calls_per_minute: int = 10, refunds_per_day: int = 2, refund_cap_cents: int = 50_000):
        self.directory = directory
        self.clock = clock or partial(datetime.now, timezone.utc)
        self.calls_per_minute = calls_per_minute
        self.refunds_per_day = refunds_per_day
        self.refund_cap_cents = refund_cap_cents
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.backend = Backend(self.db, faults)
        self.approvals = Approvals(self.db)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, account TEXT, plan TEXT)")
            self.backend.initialize()
            self.approvals.initialize()
            self._upgrade_pending_approvals()
            trace.initialize(self.db)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS quota_usage (
                    account TEXT, window TEXT, used INTEGER, PRIMARY KEY (account, window)
                )
            """)
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 0:
                self._restore_quota_usage()
                self.approvals.upgrade_votes()
                self.db.execute("PRAGMA user_version = 1")

    def _restore_quota_usage(self) -> None:
        current_minute, current_day = _quota_windows(self.clock())
        usage = Counter()
        for row in self.db.execute("SELECT account, payload FROM events JOIN runs USING (run_id)"):
            event = json.loads(row["payload"])
            # Reads have one success event. Writes have one start event, even when retried.
            called = event["status"] == "待执行" or (event["risk"] == "read" and event["status"] == trace.SUCCESS)
            refunded = event["tool"] == "refund" and event["status"] == trace.SUCCESS
            if called or refunded:
                minute, day = _quota_windows(datetime.fromisoformat(event["time"]))
                if called and minute == current_minute:
                    usage[row["account"], minute] += 1
                if refunded and day == current_day:
                    usage[row["account"], day] += 1
        self.db.executemany("""
            INSERT INTO quota_usage VALUES (?, ?, ?)
            ON CONFLICT (account, window) DO UPDATE SET used = MAX(quota_usage.used, excluded.used)
        """, ((account, window, count) for (account, window), count in usage.items()))

    def _upgrade_pending_approvals(self) -> None:
        rows = self.db.execute("""
            SELECT approvals.run_id, step, plan FROM approvals JOIN runs USING (run_id)
            WHERE status = ? AND required IS NULL
        """, (PENDING,)).fetchall()
        for row in rows:
            amount = json.loads(row["plan"])[row["step"] - 1]["params"]["amount"]
            self.db.execute("UPDATE approvals SET required = ? WHERE run_id = ? AND step = ?",
                            (self._required_approvals(amount), row["run_id"], row["step"]))

    def _required_approvals(self, amount) -> int:
        return 2 if money_cents(amount) > self.refund_cap_cents else 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def submit(self, account: str, calls: list[Call], *, run_id: str | None = None) -> dict:
        if run_id is None:
            run_id = uuid4().hex
        trace.validate_run_id(run_id)
        stored_calls = []
        for step, call in enumerate(calls, 1):
            name = trace.safe_tool(call.tool)
            tool = TOOLS.get(name)
            params = trace.stored_params(call.params, tool.params) if tool else {}
            depends_on = [dep if type(dep) is int and 1 <= dep < step else None for dep in call.depends_on]
            stored_call = {"tool": name, "params": params, "depends_on": depends_on}
            if tool and isinstance(call.args_from, dict) and call.args_from:
                stored_call["args_from"] = {
                    arg if arg in tool.params else "<无效参数>": (
                        dep if type(dep) is int and 1 <= dep < step else None, trace.safe_tool(field),
                    )
                    for arg, (dep, field) in call.args_from.items()
                }
            elif call.args_from:
                stored_call["args_from"] = [
                    dep if type(dep) is int and 1 <= dep < step else None for dep in call.args_from
                ]
            stored_calls.append(stored_call)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO runs VALUES (?, ?, ?)",
                (run_id, trace.safe_account(account), json.dumps(stored_calls, ensure_ascii=False)),
            )
            self._advance(run_id)
        return trace.replay(self.directory, run_id)

    def run(self, run_id: str) -> dict:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._advance(run_id)
        return trace.replay(self.directory, run_id)

    def decide(self, run_id: str, step: int, decision: str, *, approver: str = "default") -> dict:
        approver = approver.strip().casefold()
        if not approver:
            raise ValueError("审批人不能为空")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.approvals.decide(run_id, step, self.clock(), decision, approver=approver)
            self._advance(run_id)
        return trace.replay(self.directory, run_id)

    def queue(self) -> list[dict]:
        expired = set()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for row in self.approvals.all():
                if row["status"] == PENDING:
                    status = self.approvals.decide(row["run_id"], row["step"], self.clock())
                    if status == EXPIRED:
                        expired.add(row["run_id"])
                        self._advance(row["run_id"])
            rows = []
            for approval in self.approvals.all():
                row = dict(approval)
                if row.pop("required") == 2:
                    row["approvals_required"] = 2
                    row["approvals_received"] = self.approvals.count_votes(row["run_id"], row["step"])
                rows.append(row)
        for run_id in sorted(expired):
            trace.publish(self.directory, self.db, run_id)
        return rows

    def _advance(self, run_id: str) -> None:
        trace.validate_run_id(run_id)
        run = self.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("运行号不存在")
        calls = [Call(**item) for item in json.loads(run["plan"])]
        events = trace.latest(self.db, run_id)
        if any(call.tool not in TOOLS for call in calls):
            if not events:
                for step, call in enumerate(calls, 1):
                    if call.tool in TOOLS:
                        self._record(run_id, step, call, trace.UNEXECUTED, error="计划含白名单外的工具")
                    else:
                        self._record(run_id, step, call, REJECTED, error="工具不在白名单")
            return

        for step, call in enumerate(calls, 1):
            event = events.get(step)
            if event is None or event["status"] == PENDING:
                event = events[step] = self._step(run_id, step, call, run["account"], events)
            status = event["status"]
            if status == PENDING:
                break
            if status == REJECTED and TOOLS[call.tool].risk == "high":
                approval = self.approvals.get(run_id, step)
                if approval and approval["status"] == REJECTED:
                    for later_step, later_call in enumerate(calls[step:], step + 1):
                        if later_step not in events:
                            self._record(
                                run_id, later_step, later_call, trace.UNEXECUTED, error="本次运行的审批已拒绝",
                            )
                    break

    def _step(self, run_id: str, step: int, call: Call, account: str, events: dict[int, dict]) -> dict:
        record = partial(self._record, run_id, step, call)
        field_sources = call.args_from if isinstance(call.args_from, dict) else {}
        legacy_sources = () if isinstance(call.args_from, dict) else call.args_from
        sources = (*call.depends_on, *legacy_sources, *(dep for dep, _ in field_sources.values()))
        if any(type(dep) is not int or dep < 1 or dep >= step for dep in sources):
            return record(REJECTED, error="依赖步骤无效")
        if any(SUSPICIOUS_INSTRUCTION in events.get(dep, {}).get("flags", ()) for dep in legacy_sources):
            return record(REJECTED, error="参数来自带可疑指令的工具结果")
        if any(events.get(dep, {}).get("status") != trace.SUCCESS for dep in sources):
            return record(trace.UNEXECUTED, error="依赖步骤未成功")

        try:
            if field_sources and isinstance(call.params, dict):
                resolved = {arg: self._result_argument(events[dep], field)
                            for arg, (dep, field) in field_sources.items()}
                call = replace(call, params={**call.params, **resolved})
                record = partial(self._record, run_id, step, call)
            validate(self.backend, account, call.tool, call.params)
        except Rejected as exc:
            return record(REJECTED, error=str(exc))

        tool = TOOLS[call.tool]
        if tool.risk == "high":
            previous = events.get(step)
            if previous is None:
                required = self._required_approvals(call.params["amount"])
                self.approvals.enqueue(run_id, step, self.clock(), required)
                return record(PENDING)
            approval = self.approvals.decide(run_id, step, self.clock())
            if approval == PENDING:
                if self.approvals.count_votes(run_id, step) != previous.get("approvals_received", 0):
                    return record(PENDING)
                return previous
            if approval != APPROVED:
                return record(approval, error="审批未通过")

        minute, day = _quota_windows(self.clock())
        is_refund = call.tool == "refund"
        if is_refund and self._quota_used(account, day) >= self.refunds_per_day:
            return record(REJECTED, error=f"该账户每天最多退款 {self.refunds_per_day} 次")
        if self._quota_used(account, minute) >= self.calls_per_minute:
            return record(REJECTED, error=f"该账户每分钟最多调用 {self.calls_per_minute} 次工具")
        self._use_quota(account, minute)

        handler = partial(tool.handler, self.backend)
        if tool.risk == "read":
            return record(trace.SUCCESS, result=handler(**call.params))

        def succeeded(result, **details):
            if is_refund:
                self._use_quota(account, day)
            return record(trace.SUCCESS, result=result, **details)

        record("待执行")
        key = f"{run_id}:{step}"
        for attempt in range(1, 4):
            if attempt > 1:
                record("重试中", attempt=attempt)
            try:
                result = self.backend.execute(key, handler, **call.params)
            except TimeoutError:
                record("结果不明", error="业务系统超时", attempt=attempt)
                result = self.backend.result(key)
                if result is not None:
                    return succeeded(result, attempt=attempt, source="幂等查询")
                record("查询无记录", attempt=attempt, source="幂等查询")
            except BackendError as exc:
                return record(trace.FAILED, error=str(exc), attempt=attempt)
            else:
                return succeeded(result, attempt=attempt)
        return record(trace.REVIEW, error="初次执行和两次重试均无法确认结果")

    @staticmethod
    def _result_argument(event: dict, field: str):
        result = event["result"] or {}
        value = result.get(field)
        # Lookup notes are omitted from the audit result, so check their marker before reading it.
        marked_note = field == "note" and SUSPICIOUS_INSTRUCTION in event.get("flags", ())
        if marked_note or has_suspicious_instruction(value):
            raise Rejected("参数来自带可疑指令的工具结果")
        if field not in result:
            raise Rejected("工具结果缺少参数来源字段")
        return value

    def _quota_used(self, account: str, window: str) -> int:
        row = self.db.execute("SELECT used FROM quota_usage WHERE account = ? AND window = ?",
                              (account, window)).fetchone()
        return row["used"] if row else 0

    def _use_quota(self, account: str, window: str) -> None:
        self.db.execute("""
            INSERT INTO quota_usage VALUES (?, ?, 1)
            ON CONFLICT (account, window) DO UPDATE SET used = used + 1
        """, (account, window))

    def _record(self, run_id: str, step: int, call: Call, status: str, *,
                error=None, result=None, attempt=None, source=None) -> dict:
        tool = TOOLS.get(call.tool)
        approval = self.approvals.get(run_id, step) if tool and tool.risk == "high" else None
        event = {
            "time": self.clock().isoformat(),
            "run_id": run_id,
            "step": step,
            "tool": trace.safe_tool(call.tool),
            "params": trace.safe_params(call.params, tool.params) if tool else {},
            "risk": tool.risk if tool else None,
            "approval": approval["status"] if approval else None,
            "status": status,
            "result": result,
            "error": error,
        }
        if attempt is not None:
            event["attempt"] = attempt
        if source is not None:
            event["source"] = source
        if approval and approval["required"] == 2:
            event["approvals_required"] = approval["required"]
            event["approvals_received"] = self.approvals.count_votes(run_id, step)
        if has_suspicious_instruction(result):
            event["flags"] = [SUSPICIOUS_INSTRUCTION]
        if isinstance(result, dict) and "note" in result:
            event["result"] = {key: value for key, value in result.items() if key != "note"}
        trace.record(self.db, event)
        return event

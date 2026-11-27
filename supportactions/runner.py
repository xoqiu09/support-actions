import json
import sqlite3
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from uuid import uuid4

from . import trace
from .approvals import APPROVED, EXPIRED, PENDING, REJECTED, Approvals
from .backend import Backend, BackendError
from .planner import Call
from .tools import TOOLS, Rejected, validate


class Runner:
    def __init__(self, directory: Path, *, clock=None, faults: dict[str, str] | None = None):
        self.directory = directory
        self.clock = clock or partial(datetime.now, timezone.utc)
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
            trace.initialize(self.db)

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
            stored_calls.append({"tool": name, "params": params, "depends_on": depends_on})
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

    def decide(self, run_id: str, step: int, decision: str) -> dict:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.approvals.decide(run_id, step, self.clock(), decision)
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
            rows = [dict(row) for row in self.approvals.all()]
        for run_id in sorted(expired):
            trace.publish(self.directory, self.db, run_id)
        return rows

    def _advance(self, run_id: str) -> None:
        trace.validate_run_id(run_id)
        run = self.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("运行号不存在")
        calls = [Call(**item) for item in json.loads(run["plan"])]
        states = trace.latest(self.db, run_id)
        if any(call.tool not in TOOLS for call in calls):
            if not states:
                for step, call in enumerate(calls, 1):
                    if call.tool in TOOLS:
                        self._record(run_id, step, call, trace.UNEXECUTED, error="计划含白名单外的工具")
                    else:
                        self._record(run_id, step, call, REJECTED, error="工具不在白名单")
            return

        for step, call in enumerate(calls, 1):
            status = states.get(step)
            if status is None or status == PENDING:
                status = states[step] = self._step(run_id, step, call, run["account"], states)
            if status == PENDING:
                break
            if status == REJECTED and TOOLS[call.tool].risk == "high":
                approval = self.approvals.get(run_id, step)
                if approval and approval["status"] == REJECTED:
                    for later_step, later_call in enumerate(calls[step:], step + 1):
                        if later_step not in states:
                            self._record(
                                run_id, later_step, later_call, trace.UNEXECUTED, error="本次运行的审批已拒绝",
                            )
                    break

    def _step(self, run_id: str, step: int, call: Call, account: str, states: dict) -> str:
        record = partial(self._record, run_id, step, call)
        if any(type(dep) is not int or dep < 1 or dep >= step for dep in call.depends_on):
            return record(REJECTED, error="依赖步骤无效")
        if any(states.get(dep) != trace.SUCCESS for dep in call.depends_on):
            return record(trace.UNEXECUTED, error="依赖步骤未成功")

        try:
            validate(self.backend, account, call.tool, call.params)
        except Rejected as exc:
            return record(REJECTED, error=str(exc))

        tool = TOOLS[call.tool]
        if tool.risk == "high":
            if states.get(step) is None:
                self.approvals.enqueue(run_id, step, self.clock())
                return record(PENDING)
            approval = self.approvals.decide(run_id, step, self.clock())
            if approval == PENDING:
                return PENDING
            if approval != APPROVED:
                return record(approval, error="审批未通过")
            record("待执行")

        handler = partial(tool.handler, self.backend)
        if tool.risk == "read":
            return record(trace.SUCCESS, result=handler(**call.params))

        if tool.risk == "low":
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
                    return record(trace.SUCCESS, result=result, attempt=attempt, source="幂等查询")
                record("查询无记录", attempt=attempt, source="幂等查询")
            except BackendError as exc:
                return record(trace.FAILED, error=str(exc), attempt=attempt)
            else:
                return record(trace.SUCCESS, result=result, attempt=attempt)
        return record(trace.REVIEW, error="初次执行和两次重试均无法确认结果")

    def _record(self, run_id: str, step: int, call: Call, status: str, *,
                error=None, result=None, attempt=None, source=None) -> str:
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
        trace.record(self.db, event)
        return status

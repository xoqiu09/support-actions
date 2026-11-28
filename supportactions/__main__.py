import argparse
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from . import trace
from .approvals import APPROVED, REJECTED
from .planner import Call, plan
from .runner import Runner


def demo(out: Path) -> None:
    now = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    with TemporaryDirectory() as directory:
        with Runner(
            Path(directory), clock=lambda: now, faults={"demo-timeout:1": "timeout_after_success"},
        ) as runner:
            def ask(run_id: str, message: str) -> dict:
                return runner.submit("A", plan(message, "A"), run_id=run_id)

            lookup = ask("demo-lookup", "查订单 A-101")
            change = ask("demo-address", "A-101 改地址 青柚路9287号")
            ask("demo-refund", "我的订单 A-102 退款 199 元")
            refund = runner.decide("demo-refund", 1, APPROVED)
            duplicate = runner.decide("demo-refund", 1, APPROVED)
            duplicate["refund_count"] = runner.backend.refund_count("A-102")
            ask("demo-timeout", "我的订单 A-101 退款 39 元")
            timeout = runner.decide("demo-timeout", 1, APPROVED)
            forbidden = ask("demo-forbidden", "查订单 B-201")
            invalid = ask("demo-invalid", "坏计划")

            injection = runner.submit("B", [
                *plan("查订单 B-202", "B"),
                *plan("查订单 B-203", "B"),
                Call("refund", {}, args_from={"order_id": (2, "order_id"), "amount": (2, "note")}),
            ], run_id="demo-injection")

            now = now.replace(hour=23, minute=58, second=59)
            minute_limit = runner.submit("A", plan("查订单 A-101", "A") * 11, run_id="demo-minute-limit")
            now += timedelta(seconds=1)
            minute_reset = ask("demo-minute-reset", "查订单 A-101")

            runner.submit("B", plan("B-201 退款 10 元", "B") * 3, run_id="demo-day-limit")
            for step in (1, 2, 3):
                day_limit = runner.decide("demo-day-limit", step, APPROVED)
            now += timedelta(minutes=1)
            runner.submit("B", plan("B-201 退款 10 元", "B"), run_id="demo-day-reset")
            day_reset = runner.decide("demo-day-reset", 1, APPROVED)

            ask("demo-cap", "A-103 退款 600 元")
            runner.decide("demo-cap", 1, APPROVED, approver="reviewer-a")
            repeated = runner.decide("demo-cap", 1, APPROVED, approver="reviewer-a")
            cap = runner.decide("demo-cap", 1, APPROVED, approver="reviewer-b")
            cap["duplicate_approval_status"] = repeated["status"]

    cases = [
        ("正常查单", lookup), ("改地址", change), ("退款审批", refund),
        ("重复批准", duplicate), ("超时后查询确认", timeout),
        ("越权被拒", forbidden), ("坏计划被拒", invalid),
    ]
    m2_cases = [
        ("可疑订单备注", injection), ("每分钟工具限额", minute_limit), ("分钟限额重置", minute_reset),
        ("每日退款限额", day_limit), ("每日限额重置", day_reset), ("大额退款双人审批", cap),
    ]
    export = {
        "brand": "青柚家电",
        "scenarios": [{"name": name, **run} for name, run in cases],
        "m2_scenarios": [{"name": name, **run} for name, run in m2_cases],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out = out.resolve()
    try:
        previous_mode = stat.S_IMODE(out.stat().st_mode)
    except FileNotFoundError:
        previous_mode = None

    temporary = out.with_name(f".{uuid4().hex}.tmp")
    creation_mode = 0o666 if previous_mode is None else 0o600
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, creation_mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(export, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if previous_mode is not None:
            temporary.chmod(previous_mode)
        temporary.replace(out)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"已写入演示 JSON（{len(cases)} 个场景）")
    print(f"另含 {len(m2_cases)} 个 M2 场景")


def print_run(run: dict, *, replay: bool = False) -> None:
    print("运行号", run["run_id"])
    for step in run["events"] if replay else run["steps"]:
        if replay:
            print(json.dumps(step, ensure_ascii=False, sort_keys=True))
            continue
        message = f"{step['step']} {step['tool']} {step['status']}"
        if step["error"]:
            message += f"（{step['error']}）"
        elif step["result"] is not None:
            message += " " + json.dumps(step["result"], ensure_ascii=False, sort_keys=True)
        if "flags" in step:
            message += f"（{'、'.join(step['flags'])}）"
        if "approvals_required" in step:
            message += f"（批准 {step['approvals_received']}/{step['approvals_required']}）"
        print(message)
    print("最终状态", run["status"])
    if "trace_warning" in run:
        print(run["trace_warning"], file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python3 -m supportactions")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask")
    ask.add_argument("--account", required=True, choices=("A", "B"))
    ask.add_argument("message")
    commands.add_parser("approvals")
    for name in ("approve", "reject", "replay"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name != "replay":
            command.add_argument("step", type=int)
        if name == "approve":
            command.add_argument("--approver", default="default")
    example = commands.add_parser("demo")
    example.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    directory = Path(".support-actions")
    try:
        if args.command == "demo":
            demo(args.out)
        elif args.command == "replay":
            print_run(trace.replay(directory, args.run_id), replay=True)
        else:
            with Runner(directory) as runner:
                if args.command == "ask":
                    print_run(runner.submit(args.account, plan(args.message, args.account)))
                elif args.command == "approvals":
                    queue = runner.queue()
                    if not queue:
                        print("审批队列为空")
                    for item in queue:
                        message = f"{item['run_id']} {item['step']} {item['status']} 截止 {item['deadline']}"
                        if "approvals_required" in item:
                            message += f"（批准 {item['approvals_received']}/{item['approvals_required']}）"
                        print(message)
                else:
                    decision = APPROVED if args.command == "approve" else REJECTED
                    print_run(runner.decide(
                        args.run_id, args.step, decision, approver=getattr(args, "approver", "default"),
                    ))
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

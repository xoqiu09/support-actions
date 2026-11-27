import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from . import trace
from .approvals import APPROVED, REJECTED
from .planner import plan
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

    cases = [
        ("正常查单", lookup), ("改地址", change), ("退款审批", refund),
        ("重复批准", duplicate), ("超时后查询确认", timeout),
        ("越权被拒", forbidden), ("坏计划被拒", invalid),
    ]
    export = {"brand": "青柚家电", "scenarios": [{"name": name, **run} for name, run in cases]}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(export, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"已写入演示 JSON（{len(cases)} 个场景）")


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
        print(message)
    print("最终状态", run["status"])


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
                        print(item["run_id"], item["step"], item["status"], "截止", item["deadline"])
                else:
                    decision = APPROVED if args.command == "approve" else REJECTED
                    print_run(runner.decide(args.run_id, args.step, decision))
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

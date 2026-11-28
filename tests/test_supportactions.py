import errno
import io
import json
import os
import stat
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, DEFAULT, Mock, call, patch
from zoneinfo import ZoneInfo

from supportactions import trace
from supportactions import __main__ as cli_module
from supportactions.approvals import APPROVED, EXPIRED, PENDING, REJECTED, TTL, Approvals
from supportactions.backend import Backend
from supportactions.planner import Call, plan
from supportactions.runner import Runner


ROOT = Path(__file__).resolve().parents[1]
REFUND = "我的订单 A-102 退款 199 元"


class SupportActionsTests(unittest.TestCase):
    def setUp(self):
        scratch = self.enter_tempdir()
        self.directory = scratch / ".support-actions"
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def enter_tempdir(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        return Path(scratch.name)

    def runner(self, **kwargs):
        return Runner(self.directory, clock=lambda: self.now, **kwargs)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "supportactions", *args], cwd=self.directory.parent,
            env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, check=True,
            text=True, encoding="utf-8",
        ).stdout

    def test_allow_list_blocks_entire_run(self):
        with self.runner() as runner:
            statements = []
            runner.db.set_trace_callback(statements.append)
            run = runner.submit("A", plan("坏计划", "A"))
            self.assertEqual([step["status"] for step in run["steps"]], [trace.UNEXECUTED, REJECTED])
            self.assertEqual(run["steps"][1]["error"], "工具不在白名单")
            self.assertFalse(any("FROM orders" in sql for sql in statements))

    def test_foreign_order_blocks_dependent_step(self):
        calls = [
            Call("lookup_order", {"order_id": "B-201"}),
            Call("change_address", {"order_id": "A-101", "new_address": "青柚路9287号"}, (1,)),
            Call("list_orders", {"account": "A"}),
            Call("list_orders", {"account": "B"}),
        ]
        with self.runner() as runner:
            run = runner.submit("A", calls)
            self.assertEqual(
                [step["status"] for step in run["steps"]],
                [REJECTED, trace.UNEXECUTED, trace.SUCCESS, REJECTED],
            )
            self.assertEqual(run["steps"][0]["error"], "无权操作该订单")
            self.assertEqual(run["steps"][1]["error"], "依赖步骤未成功")
            self.assertEqual(runner.backend.order("A-101")["address"], "青柚南路***")
            orders = run["steps"][2]["result"]["orders"]
            self.assertEqual([row["order_id"] for row in orders], ["A-101", "A-102", "A-103"])

    def test_invalid_arguments_block_only_that_step_and_its_dependents(self):
        calls = [
            Call("lookup_order", {}),
            Call("lookup_order", {"order_id": 102}),
            Call("refund", {"order_id": "A-102", "amount": True}),
            Call("list_orders", {"account": "A"}, (2,)),
            Call("lookup_order", {"order_id": "A-102"}),
            *plan("A-102 退款 199.0000000000000001 元", "A"),
        ]
        with self.runner() as runner:
            run = runner.submit("A", calls)
            self.assertEqual(
                [step["status"] for step in run["steps"]],
                [REJECTED, REJECTED, REJECTED, trace.UNEXECUTED, trace.SUCCESS, REJECTED],
            )
            self.assertEqual(runner.queue(), [])

    def test_overpayment_and_shipped_order_are_rejected(self):
        calls = [
            Call("refund", {"order_id": "A-102", "amount": 199.01}),
            Call("change_address", {"order_id": "A-103", "new_address": "青柚路9287号"}),
        ]
        with self.runner() as runner:
            run = runner.submit("A", calls)
            self.assertEqual([step["status"] for step in run["steps"]], [REJECTED, REJECTED])
            self.assertEqual([step["error"] for step in run["steps"]], ["退款金额超过实付", "订单已发货"])
            self.assertEqual(runner.queue(), [])
            self.assertEqual(runner.backend.refund_count("A-102"), 0)
            self.assertEqual(runner.backend.order("A-103")["address"], "青柚东路***")

    def test_address_questions_do_not_change_the_order(self):
        with self.runner() as runner:
            before = runner.backend.order("A-101")["address"]
            for message in ("A-101 可以改地址吗？", "A-101 改地址 吗？", "A-101 改地址 可以吗"):
                with self.subTest(message=message):
                    run = runner.submit("A", plan(message, "A"))
                    self.assertEqual(runner.backend.order("A-101")["address"], before)
                    events = trace.replay(self.directory, run["run_id"])["events"]
                    self.assertFalse(any(
                        event["tool"] == "change_address" and event["status"] == trace.SUCCESS
                        for event in events
                    ))

    def test_address_change_without_new_address_keeps_independent_lookup(self):
        with self.runner() as runner:
            before = runner.backend.order("A-101")["address"]
            calls = plan("查订单 A-102", "A") + plan("A-101 改地址", "A")
            lookup, change = runner.submit("A", calls)["steps"]
            self.assertEqual(lookup["status"], trace.SUCCESS)
            self.assertEqual(lookup["result"]["order_id"], "A-102")
            self.assertEqual(change["tool"], "change_address")
            self.assertEqual(change["status"], REJECTED)
            self.assertEqual(change["error"], "工具参数缺失或包含未定义字段")
            self.assertEqual(runner.backend.order("A-101")["address"], before)

    def test_refund_rejects_multiple_distinct_order_ids(self):
        with self.runner() as runner:
            run = runner.submit("A", plan("查订单 A-101，给 A-102 退款 39 元", "A"))
            self.assertEqual(runner.queue(), [])
            self.assertEqual(run["steps"][0]["status"], REJECTED)
            self.assertEqual(runner.backend.refund_count("A-101"), 0)
            self.assertEqual(runner.backend.refund_count("A-102"), 0)

            same_order = runner.submit("A", plan("查订单 A-102，给 A-102 退款 39 元", "A"))
            self.assertEqual(same_order["steps"][0]["status"], PENDING)

    def test_negated_refund_does_not_enter_approval_queue(self):
        with self.runner() as runner:
            runner.submit("A", plan("A-102 不要退款 39 元", "A"))
            self.assertEqual(runner.backend.refund_count("A-102"), 0)
            self.assertEqual(len(runner.queue()), 0)

    def test_affirmative_refund_enters_approval_queue(self):
        with self.runner() as runner:
            run = runner.submit("A", plan("A-102 退款 39 元", "A"))
            self.assertEqual([(row["run_id"], row["status"]) for row in runner.queue()], [
                (run["run_id"], PENDING),
            ])
            self.assertEqual(runner.backend.refund_count("A-102"), 0)

    def test_refund_waits_for_approval_and_runs_once_after_restart(self):
        pending = self.cli("ask", "--account", "A", REFUND)
        self.assertIn("1 refund 待审", pending)
        run_id = pending.splitlines()[0].split()[1]
        with self.runner() as runner:
            self.assertEqual(runner.backend.refund_count("A-102"), 0)
        self.assertIn(f"{run_id} 1 待审", self.cli("approvals"))
        approved = self.cli("approve", run_id, "1")
        self.assertIn("1 refund 成功", approved)
        self.assertEqual(self.cli("approve", run_id, "1"), approved)
        with self.runner() as runner:
            self.assertEqual(runner.run(run_id)["status"], trace.SUCCESS)
            self.assertEqual(runner.backend.refund_count("A-102"), 1)

    def test_approval_reports_committed_refund_when_trace_write_fails(self):
        run_id = "trace-write-failure"
        with Runner(self.directory) as runner:
            runner.submit("A", plan("A-102 退款 39 元", "A"), run_id=run_id)

        stdout, stderr = StringIO(), StringIO()
        with (
            patch.object(cli_module, "Path", return_value=self.directory),
            patch.object(sys, "argv", ["supportactions", "approve", run_id, "1"]),
            patch.object(Path, "replace", side_effect=OSError("模拟审计文件替换失败")),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            cli_module.main()
        self.assertIn(f"运行号 {run_id}", stdout.getvalue())
        self.assertIn('1 refund 成功 {"order_id": "A-102", "refunded_cents": 3900}', stdout.getvalue())
        self.assertIn("审计文件写入失败", stderr.getvalue())

        replay = self.cli("replay", run_id).splitlines()
        self.assertEqual(replay[-1], "最终状态 成功")
        events = (self.directory / "traces" / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(events, replay[1:-1])
        self.assertEqual(self.cli("approve", run_id, "1"), stdout.getvalue())
        with self.runner() as runner:
            self.assertEqual(runner.backend.refund_count("A-102"), 1)

    def test_approved_refunds_cannot_exceed_remaining_amount(self):
        with self.runner() as runner:
            first = runner.submit("A", plan(REFUND, "A"))["run_id"]
            second = runner.submit("A", plan(REFUND, "A"))["run_id"]
            self.assertEqual([row["status"] for row in runner.queue()], [PENDING, PENDING])
            self.assertEqual(runner.decide(first, 1, APPROVED)["status"], trace.SUCCESS)
        with self.runner() as restarted:
            blocked = restarted.decide(second, 1, APPROVED)
            self.assertEqual(blocked["status"], trace.FAILED)
            self.assertEqual(blocked["steps"][0]["error"], "退款金额超过剩余可退金额")
            rows = restarted.db.execute("SELECT order_id, SUM(amount_cents), COUNT(*) FROM refunds GROUP BY order_id")
            self.assertEqual([tuple(row) for row in rows], [("A-102", 19900, 1)])

    def test_refund_steps_keep_distinct_keys_and_exact_amounts_after_restart(self):
        calls = [
            Call("refund", {"order_id": "A-102", "amount": 77.13}),
            Call("refund", {"order_id": "A-102", "amount": 121.87}, (1,)),
        ]
        with self.runner() as runner:
            run_id = runner.submit("A", calls)["run_id"]
            first = runner.decide(run_id, 1, APPROVED)
            self.assertEqual([step["status"] for step in first["steps"]], [trace.SUCCESS, PENDING])
            complete = runner.decide(run_id, 2, APPROVED)
            self.assertEqual([step["status"] for step in complete["steps"]], [trace.SUCCESS, trace.SUCCESS])
        with self.runner() as restarted:
            self.assertEqual(restarted.run(run_id), complete)
            for step in (1, 2):
                self.assertEqual(restarted.decide(run_id, step, APPROVED), complete)
            rows = restarted.db.execute("SELECT order_id, amount_cents FROM refunds ORDER BY amount_cents")
            self.assertEqual([tuple(row) for row in rows], [("A-102", 7713), ("A-102", 12187)])
            keys = restarted.db.execute("SELECT effect_key FROM effects ORDER BY effect_key")
            self.assertEqual([row[0] for row in keys], [f"{run_id}:1", f"{run_id}:2"])

    def test_backend_retries_same_effect_key_once_even_after_restart(self):
        with self.runner() as runner:
            pending = runner.submit("A", plan("A-102 退款 50 元", "A"))
            run_id = pending["run_id"]
            result = runner.decide(run_id, 1, APPROVED)["steps"][0]["result"]
            with runner.db:
                runner.db.execute("BEGIN IMMEDIATE")
                retry = runner.backend.execute(
                    f"{run_id}:1", runner.backend.refund, order_id="A-102", amount_cents=5000,
                )
            self.assertEqual(retry, result)
            self.assertEqual(runner.backend.refund_count("A-102"), 1)
        with self.runner() as restarted:
            with restarted.db:
                restarted.db.execute("BEGIN IMMEDIATE")
                retry = restarted.backend.execute(
                    f"{run_id}:1", restarted.backend.refund, order_id="A-102", amount_cents=5000,
                )
            self.assertEqual(retry, result)
            self.assertEqual(restarted.backend.refund_count("A-102"), 1)

    def test_invalid_approval_decision_leaves_refund_pending(self):
        with self.runner() as runner:
            run = runner.submit("A", plan("A-102 退款 39 元", "A"))
            run_id = run["run_id"]
            with self.assertRaisesRegex(ValueError, "审批决定"):
                runner.decide(run_id, 1, "同意")
            self.assertEqual(runner.queue()[0]["status"], PENDING)
            self.assertEqual(runner.run(run_id), run)
            self.assertEqual(runner.backend.refund_count("A-102"), 0)

            approved = runner.decide(run_id, 1, APPROVED)
            self.assertEqual(approved["steps"][0]["result"]["refunded_cents"], 3900)
            self.assertEqual(runner.backend.refund_count("A-102"), 1)

    def test_invalid_approval_storage_decision_can_be_followed_by_approval(self):
        with self.runner() as runner:
            run_id = runner.submit("A", plan("A-102 退款 39 元", "A"))["run_id"]
            for decision in ("同意", PENDING, EXPIRED):
                with self.subTest(decision=decision):
                    with self.assertRaisesRegex(ValueError, "审批决定"), runner.db:
                        runner.approvals.decide(run_id, 1, self.now, decision)
                    self.assertEqual(runner.approvals.get(run_id, 1)["status"], PENDING)
            self.assertEqual(runner.approvals.decide(run_id, 1, self.now), PENDING)
            self.assertEqual(runner.backend.refund_count("A-102"), 0)

            approved = runner.decide(run_id, 1, APPROVED)
            self.assertEqual(approved["steps"][0]["result"]["refunded_cents"], 3900)
            self.assertEqual(runner.decide(run_id, 1, APPROVED), approved)
            self.assertEqual(runner.backend.refund_count("A-102"), 1)

    def test_expired_or_rejected_approval_never_executes(self):
        with self.runner() as runner:
            rejected = runner.submit("A", plan(REFUND, "A"))["run_id"]
            expired = runner.submit("A", plan(REFUND, "A"))["run_id"]
            expired_on_queue = runner.submit("A", plan(REFUND, "A"))["run_id"]
            self.assertEqual(runner.decide(rejected, 1, REJECTED)["status"], REJECTED)
            self.assertEqual(runner.decide(rejected, 1, APPROVED)["status"], REJECTED)
            self.now += timedelta(minutes=5)
            self.assertEqual(runner.decide(expired, 1, APPROVED)["status"], EXPIRED)
            self.assertEqual({row["status"] for row in runner.queue()}, {EXPIRED, REJECTED})
            self.assertEqual(trace.replay(self.directory, expired_on_queue)["status"], EXPIRED)
            self.assertEqual(runner.backend.refund_count("A-102"), 0)

    def test_refund_approval_expires_after_five_minutes_across_clock_rollback(self):
        local_zone = ZoneInfo("America/Los_Angeles")
        start = datetime(2026, 11, 1, 8, 58, tzinfo=timezone.utc)
        deadline = start + timedelta(minutes=5)
        self.now = start.astimezone(local_zone)
        with self.runner() as runner:
            run_id = runner.submit("A", plan("A-102 退款 39 元", "A"))["run_id"]
            self.now = (deadline - timedelta(seconds=1)).astimezone(local_zone)
            self.assertEqual(runner.queue()[0]["status"], PENDING)

            self.now = deadline.astimezone(local_zone)
            result = runner.decide(run_id, 1, APPROVED)
            self.assertEqual((result["status"], runner.backend.refund_count("A-102")), (EXPIRED, 0))

    def test_legacy_refund_approval_expires_after_clock_rollback_and_reopen(self):
        def enqueue_with_local_deadline(approvals, run_id, step, now, required):
            approvals.db.execute("INSERT INTO approvals VALUES (?, ?, ?, ?, ?)",
                                 (run_id, step, PENDING, (now + TTL).isoformat(), required))

        local_zone = ZoneInfo("America/Los_Angeles")
        start = datetime(2026, 11, 1, 8, 58, tzinfo=timezone.utc)
        self.now = start.astimezone(local_zone)
        with self.runner() as runner, patch.object(Approvals, "enqueue", enqueue_with_local_deadline):
            run_id = runner.submit("A", plan("A-102 退款 39 元", "A"))["run_id"]
            runner.db.execute("PRAGMA user_version = 1")

        deadline = start + timedelta(minutes=5)
        self.now = (deadline - timedelta(seconds=1)).astimezone(local_zone)
        with self.runner() as runner:
            queued = runner.queue()[0]
            self.assertEqual(queued["status"], PENDING)

        self.now = deadline.astimezone(local_zone)
        with self.runner() as runner:
            result = runner.decide(run_id, 1, APPROVED)
            self.assertEqual((result["status"], runner.backend.refund_count("A-102")), (EXPIRED, 0))
            self.assertEqual(datetime.fromisoformat(queued["deadline"]), deadline)

    def test_rejected_approval_stops_remaining_independent_steps(self):
        calls = [
            *plan(REFUND, "A"),
            *plan("A-101 改地址 青柚街9287号", "A"),
            *plan("A-101 退款 39 元", "A"),
        ]
        with self.runner() as runner:
            original_address = runner.backend.order("A-101")["address"]
            run_id = runner.submit("A", calls)["run_id"]
            rejected = runner.decide(run_id, 1, REJECTED)
            self.assertEqual(rejected["status"], REJECTED)
            self.assertEqual([step["status"] for step in rejected["steps"]],
                             [REJECTED, trace.UNEXECUTED, trace.UNEXECUTED])
            self.assertEqual(runner.backend.order("A-101")["address"], original_address)
            self.assertEqual(runner.backend.refund_count("A-102"), 0)
            self.assertEqual([(row["step"], row["status"]) for row in runner.queue()], [(1, REJECTED)])
        with self.runner() as restarted:
            self.assertEqual(restarted.run(run_id), rejected)

    def test_timeout_after_effect_is_confirmed_by_key_without_retry(self):
        with self.runner(faults={"test-timeout:1": "timeout_after_success"}) as runner:
            pending = runner.submit("A", plan(REFUND, "A"), run_id="test-timeout")
            with patch.object(runner.backend, "execute", wraps=runner.backend.execute) as execute:
                run = runner.decide(pending["run_id"], 1, APPROVED)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual([event["status"] for event in run["events"]], [PENDING, "待执行", "结果不明", trace.SUCCESS])
            self.assertEqual(run["steps"][0]["source"], "幂等查询")
            self.assertEqual(runner.backend.refund_count("A-102"), 1)

    def test_second_timeout_is_confirmed_before_another_retry(self):
        key = "test-second-timeout:1"
        with self.runner() as runner:
            run_id = runner.submit("A", plan(REFUND, "A"), run_id="test-second-timeout")["run_id"]
            # Keep the fake backend's internal key lookup separate from the runner's recovery queries.
            backend = Backend(runner.db, {key: "timeout_after_success"})
            with (
                patch.object(runner.backend, "execute") as execute,
                patch.object(runner.backend, "result", wraps=runner.backend.result) as result,
            ):
                def attempt(key, handler, **params):
                    if execute.call_count == 1:
                        raise TimeoutError
                    return backend.execute(key, handler, **params)

                execute.side_effect = attempt
                operations = Mock()
                operations.attach_mock(execute, "execute")
                operations.attach_mock(result, "result")
                completed = runner.decide(run_id, 1, APPROVED)
            refund = call.execute(key, ANY, amount=199.0, order_id="A-102")
            self.assertEqual(operations.mock_calls, [refund, call.result(key), refund, call.result(key)])
            self.assertEqual(completed["status"], trace.SUCCESS)
            self.assertEqual(completed["steps"][0]["attempt"], 2)
            self.assertEqual(completed["steps"][0]["source"], "幂等查询")
            self.assertEqual(runner.backend.refund_count("A-102"), 1)

    def test_unknown_timeout_tries_twice_more_then_stops(self):
        with self.runner(faults={"test-unknown:1": "unknown"}) as runner:
            pending = runner.submit("A", plan(REFUND, "A"), run_id="test-unknown")
            with patch.object(runner.backend, "execute", wraps=runner.backend.execute) as execute:
                run = runner.decide(pending["run_id"], 1, APPROVED)
                self.assertEqual(runner.decide(pending["run_id"], 1, APPROVED), run)
                self.assertEqual(execute.call_count, 3)
                self.assertEqual({call.args[0] for call in execute.call_args_list}, {"test-unknown:1"})
            self.assertEqual(run["status"], trace.REVIEW)
            self.assertEqual(
                [event["status"] for event in run["events"]],
                [PENDING, "待执行", "结果不明", "查询无记录", "重试中", "结果不明", "查询无记录",
                 "重试中", "结果不明", "查询无记录", trace.REVIEW],
            )
        with self.runner() as restarted:
            self.assertEqual(restarted.run(pending["run_id"]), run)
            self.assertEqual(restarted.backend.refund_count("A-102"), 0)

    def test_refund_needing_review_stops_later_independent_steps(self):
        run_id = "review-stops-plan"
        calls = [
            *plan("A-102 退款 39 元", "A"),
            *plan("A-101 改地址 青柚路9287号", "A"),
        ]
        with self.runner(faults={f"{run_id}:1": "unknown"}) as runner:
            before = runner.backend.order("A-101")["address"]
            runner.submit("A", calls, run_id=run_id)
            run = runner.decide(run_id, 1, APPROVED)
            self.assertEqual(run["status"], trace.REVIEW)
            self.assertEqual(runner.backend.order("A-101")["address"], before)
            self.assertEqual([step["step"] for step in run["steps"]], [1])
        with self.runner() as restarted:
            self.assertEqual(restarted.run(run_id), run)
            self.assertEqual(restarted.backend.order("A-101")["address"], before)

    def test_definite_backend_failure_does_not_retry(self):
        with self.runner(faults={"test-failure:1": "failure"}) as runner:
            calls = plan("A-101 改地址 青柚路9287号", "A")
            run = runner.submit("A", calls, run_id="test-failure")
            self.assertEqual([event["status"] for event in run["events"]], ["待执行", trace.FAILED])
            self.assertEqual(runner.backend.order("A-101")["address"], "青柚南路***")

    def test_replay_matches_original_without_backend_access(self):
        with self.runner() as runner, patch.object(trace, "record", wraps=trace.record) as record:
            runner.submit("A", plan(REFUND, "A"), run_id="test-replay")
            self.now += timedelta(seconds=30)
            runner.decide("test-replay", 1, APPROVED)
        original = [recorded.args[1] for recorded in record.call_args_list]
        self.assertEqual([event["status"] for event in original], [PENDING, "待执行", trace.SUCCESS])
        names = ("__init__", "initialize", "order", "list_orders", "execute", "result", "refund",
                 "change_address", "refund_count")
        with patch.multiple(Backend, **dict.fromkeys(names, DEFAULT)) as methods:
            for name, method in methods.items():
                method.side_effect = AssertionError(f"replay called backend.{name}")
            replayed = trace.replay(self.directory, "test-replay")
            stdout = StringIO()
            with (
                patch.object(cli_module, "Path", return_value=self.directory),
                patch.object(sys, "argv", ["supportactions", "replay", "test-replay"]),
                redirect_stdout(stdout),
            ):
                cli_module.main()
        self.assertEqual(replayed["events"], original)
        self.assertEqual(replayed["steps"], [original[-1]])
        self.assertEqual(replayed["status"], trace.SUCCESS)
        output = stdout.getvalue().splitlines()
        self.assertEqual(output[0], "运行号 test-replay")
        self.assertEqual([json.loads(line) for line in output[1:-1]], original)
        self.assertEqual(output[-1], "最终状态 成功")

    def test_trace_files_keep_run_ids_that_differ_only_by_case(self):
        replace, read_text = Path.replace, Path.read_text
        destinations = []
        files_by_folded_path = {}

        def replace_ignoring_case(source, destination):
            destinations.append(destination)
            actual_path = files_by_folded_path.setdefault(str(destination).casefold(), destination)
            return replace(source, actual_path)

        def read_ignoring_case(path, *args, **kwargs):
            actual_path = files_by_folded_path.get(str(path).casefold(), path)
            return read_text(actual_path, *args, **kwargs)

        with (
            self.runner() as runner,
            patch.object(Path, "replace", replace_ignoring_case),
            patch.object(Path, "read_text", read_ignoring_case),
        ):
            runner.submit("A", plan("查订单 A-101", "A"), run_id="CaseA")
            runner.submit("A", plan("查订单 A-102", "A"), run_id="casea")
            events = [json.loads(path.read_text(encoding="utf-8")) for path in destinations]

        self.assertEqual([event["result"]["order_id"] for event in events], ["A-101", "A-102"])
        self.assertEqual(destinations[1], self.directory / "traces" / "casea.jsonl")

    def test_new_lowercase_trace_preserves_legacy_mixed_case_trace(self):
        traces = self.directory / "traces"
        legacy_path = traces / "CaseA.jsonl"
        lowercase_path = traces / "casea.jsonl"
        open_path, replace = Path.open, Path.replace

        def open_ignoring_case(path, *args, **kwargs):
            return open_path(legacy_path if path == lowercase_path else path, *args, **kwargs)

        def replace_ignoring_case(source, destination):
            return replace(source, legacy_path if destination == lowercase_path else destination)

        with self.runner() as runner:
            original = runner.submit("A", plan("查订单 A-101", "A"), run_id="CaseA")
            # Recreate the filename written by the old version using the original audit contents.
            next((traces / "case-sensitive").glob("*.jsonl")).replace(legacy_path)

        with (
            self.runner() as runner,
            patch.object(Path, "open", open_ignoring_case),
            patch.object(Path, "replace", replace_ignoring_case),
        ):
            runner.submit("A", plan("查订单 A-102", "A"), run_id="casea")
            copied_paths = sorted((traces / "case-sensitive").glob("*.jsonl"))
            events = [json.loads(path.read_text(encoding="utf-8")) for path in [*copied_paths, lowercase_path]]

        self.assertEqual([event["result"]["order_id"] for event in events], ["A-101", "A-102"])
        self.assertEqual(events[0], original["events"][0])

    def test_addresses_are_redacted_in_every_persisted_record(self):
        with self.runner() as runner:
            address_before = runner.backend.order("A-101")["address"]
            shipped_address = runner.backend.order("A-103")["address"]
            calls = [
                Call("change_address", {"order_id": "A-101", "new_address": "青柚街9287号"}),
                Call("change_address", {"order_id": "A-103", "new_address": "青柚巷9387号"}),
            ]
            run = runner.submit("A", calls)
            self.assertEqual([step["status"] for step in run["steps"]], [trace.SUCCESS, REJECTED])
            self.assertNotEqual(address_before, "青柚街***")
            self.assertEqual(runner.backend.order("A-101")["address"], "青柚街***")
            self.assertEqual(runner.backend.order("A-103")["address"], shipped_address)
            stored_plan = runner.db.execute("SELECT plan FROM runs WHERE run_id = ?", (run["run_id"],)).fetchone()[0]
            self.assertEqual([item["params"]["new_address"] for item in json.loads(stored_plan)],
                             ["青柚街***", "青柚巷***"])
            self.assertEqual([event["params"]["new_address"] for event in run["events"]],
                             ["青柚街***", "青柚街***", "青柚巷***"])
            dump = "\n".join(runner.db.iterdump())
        log = self.directory / "traces" / f"{run['run_id']}.jsonl"
        logged = log.read_text(encoding="utf-8")
        self.assertEqual([json.loads(line) for line in logged.splitlines()], run["events"])

        def demo_plan(message, account):
            if "改地址" in message:
                return calls[:1]
            if message == "查订单 B-201":
                return calls[1:]
            return plan(message, account)

        export = self.directory / "redacted-addresses.json"
        with patch.object(cli_module, "plan", side_effect=demo_plan), redirect_stdout(StringIO()):
            cli_module.demo(export)
        output = export.read_text(encoding="utf-8")
        cases = json.loads(output)["scenarios"]
        for index, status, expected in ((1, trace.SUCCESS, "青柚街***"), (5, REJECTED, "青柚巷***")):
            self.assertEqual(cases[index]["status"], status)
            self.assertEqual(cases[index]["steps"][0]["params"]["new_address"], expected)
        for private in ("9287", "9387"):
            self.assertNotIn(private, dump)
            self.assertNotIn(private, logged)
            self.assertNotIn(private, output)

    def test_unrecognized_order_and_account_numbers_are_redacted(self):
        calls = [
            Call("lookup_order", {"order_id": "A-1234567890"}),
            Call("list_orders", {"account": "9876543210"}),
        ]
        with self.runner() as runner:
            run = runner.submit("A", calls)
            self.assertEqual([step["status"] for step in run["steps"]], [REJECTED, REJECTED])
            self.assertEqual([step["params"] for step in run["steps"]],
                             [{"order_id": "<无效值>"}, {"account": "<无效值>"}])
            dump = "\n".join(runner.db.iterdump())
        log = self.directory / "traces" / f"{run['run_id']}.jsonl"
        for private in ("1234567890", "9876543210"):
            self.assertNotIn(private, dump)
            self.assertNotIn(private, log.read_text(encoding="utf-8"))

    def test_invalid_run_id_is_rejected_before_storage(self):
        with self.runner() as runner:
            for run_id in ("invalid/run", "", "has space", "a" * 250):
                with self.subTest(run_id=run_id), self.assertRaisesRegex(ValueError, "运行号"):
                    runner.submit("A", plan("查订单 A-101", "A"), run_id=run_id)
            for table in ("runs", "events"):
                self.assertEqual(runner.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertFalse((self.directory / "traces").exists())

    def test_empty_plan_is_rejected_without_reserving_run_id(self):
        with self.runner() as runner:
            with self.assertRaisesRegex(ValueError, "^计划不能为空$"):
                runner.submit("A", [], run_id="empty")
            self.assertEqual(runner.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

            run = runner.submit("A", plan("查订单 A-101", "A"), run_id="empty")
            self.assertEqual(run["status"], trace.SUCCESS)
            self.assertEqual(run["steps"][0]["result"]["order_id"], "A-101")

    def test_demo_preserves_previous_export_when_disk_fills(self):
        out = self.directory / "demo.json"
        self.cli("demo", "--out", str(out))
        original = out.read_bytes()
        original_json = json.loads(original)
        original_open = io.open
        written = []

        def open_with_disk_full(file, mode="r", *args, **kwargs):
            handle = original_open(file, mode, *args, **kwargs)
            if mode != "w":
                return handle
            opened_file = os.fstat(handle.fileno())
            if any(os.path.samestat(opened_file, entry.stat()) for entry in out.parent.iterdir()):
                write = handle.write

                def partial_write(data):
                    written.append(write(data[:12]))
                    raise OSError(errno.ENOSPC, "No space left on device")

                handle.write = partial_write
            return handle

        with patch("io.open", side_effect=open_with_disk_full), self.assertRaises(OSError) as failure:
            cli_module.demo(out)

        self.assertEqual(failure.exception.errno, errno.ENOSPC)
        self.assertEqual(written, [12])
        self.assertEqual(out.read_bytes(), original)
        self.assertEqual(json.loads(out.read_bytes()), original_json)
        self.assertEqual(list(out.parent.iterdir()), [out])

    def test_demo_preserves_existing_export_permissions(self):
        out = self.directory / "demo.json"
        out.parent.mkdir()
        out.write_text("{}", encoding="utf-8")
        out.chmod(0o644)

        self.cli("demo", "--out", str(out))

        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o644)
        self.assertEqual(json.loads(out.read_bytes())["brand"], "青柚家电")

    def test_demo_new_export_respects_umask(self):
        out = self.directory / "demo.json"
        previous_umask = os.umask(0o027)
        try:
            self.cli("demo", "--out", str(out))
        finally:
            os.umask(previous_umask)

        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o640)

    def test_demo_updates_symlink_target_without_replacing_link(self):
        out = self.directory / "demo.json"
        out.parent.mkdir()
        target = self.directory.parent / "real.json"
        target.write_text("{}", encoding="utf-8")
        link_target = Path("..") / target.name
        out.symlink_to(link_target)

        self.cli("demo", "--out", str(out))

        self.assertTrue(out.is_symlink())
        self.assertEqual(out.readlink(), link_target)
        self.assertEqual(json.loads(target.read_bytes())["brand"], "青柚家电")

    def test_trace_redacts_addresses_and_demo_is_deterministic(self):
        first = self.directory / "first.json"
        second = self.directory / "second.json"
        self.cli("demo", "--out", str(first))
        hostname, username = "fixture-host.example.test", "fixture-demo-user"
        with (
            patch.dict(os.environ, {"HOSTNAME": hostname, "COMPUTERNAME": hostname,
                                    "USER": username, "USERNAME": username, "LOGNAME": username}),
            patch("socket.gethostname", return_value=hostname),
            patch("getpass.getuser", return_value=username),
            redirect_stdout(StringIO()),
        ):
            cli_module.demo(second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        output = second.read_text(encoding="utf-8")
        for private in ("9287", "9387", "3186号", "4186号", hostname, username):
            self.assertNotIn(private, output)
        self.assertIn("青柚路***", output)

        def check_paths(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    check_paths(key)
                    check_paths(item)
            elif isinstance(value, list):
                for item in value:
                    check_paths(item)
            elif isinstance(value, str):
                self.assertNotRegex(value, r"(?<![A-Za-z0-9_./\\-])(?:/|[A-Za-z]:[/\\]|\\\\)")

        export = json.loads(output)
        check_paths(export)
        scenarios = export["scenarios"]
        self.assertEqual([item["status"] for item in scenarios], [trace.SUCCESS] * 5 + [REJECTED] * 2)
        self.assertEqual(scenarios[3]["refund_count"], 1)
        self.assertEqual(scenarios[4]["steps"][0]["source"], "幂等查询")

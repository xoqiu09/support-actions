# support-actions

青柚家电的客服团队想让助手代办查单、改址和退款，同时避免查到别人的订单、改错地址或重复退款。

- 查询当前账户的订单，给未发货的订单改地址。
- 退款须人工批准，大额退款需用两个不同的审批人标识分别批准，操作和退款都有账户限额。
- 按运行号回放已处理步骤的状态和结果，记录中会遮住门牌号。

## 怎么验证的

实跑 `/opt/homebrew/bin/python3 -m unittest`，**38 项全部通过**。测试确认越权操作和引用可疑备注的步骤会被拒绝，未经批准的退款不会入账，同一退款步骤在重复批准、超时或重启后也不会重复入账，回放不会再次调用业务系统。

在临时副本里逐项破坏订单权限、退款审批、防重复退款、可疑备注标记、账户限额和大额双人审批，对应测试均按预期失败。恢复代码后，全套测试再次通过。

## 试一下

[打开在线演示](https://xoqiu.cn/actions-demo/index.html)

从刚克隆的仓库根目录运行。第一条跑测试，第二条提交一笔待审的模拟退款。

```sh
python3 -m unittest
python3 -m supportactions ask --account A "A-102 退款 39 元"
```

演示数据均为虚构，无真实模型或用户。Owner 定需求和验收标准，指导 AI 编程工具实现。

## English summary

- A support team needs to handle order requests without exposing another customer’s orders or issuing duplicate refunds.
- The demo looks up orders, updates shipping addresses before dispatch, and puts refunds in an approval queue.
- Large refunds require approval under two distinct approver IDs. The audit trail can be replayed without calling the simulated backend.
- All 38 tests passed. Deliberately breaking key rules made the corresponding tests fail.
- The demo uses fictional data, has no live model or users, and was built with AI coding tools under the owner’s requirements and acceptance criteria.

## 运行

需要 Python 3.10 或更新版本，只用标准库。下面的示例从空的本地状态开始，在仓库根目录执行。若 Python 装在别处，把 `/opt/homebrew/bin/python3` 换成你的解释器路径。

```sh
/opt/homebrew/bin/python3 -m unittest -v
/opt/homebrew/bin/python3 -m supportactions ask --account A "我的订单"
/opt/homebrew/bin/python3 -m supportactions ask --account A "查订单 A-101"
/opt/homebrew/bin/python3 -m supportactions ask --account A "A-101 改地址 青柚路9287号"
pending=$(/opt/homebrew/bin/python3 -m supportactions ask --account A "我的订单 A-102 退款 199 元")
printf '%s\n' "$pending"
run_id=$(printf '%s\n' "$pending" | awk 'NR == 1 {print $2}')
/opt/homebrew/bin/python3 -m supportactions approvals
/opt/homebrew/bin/python3 -m supportactions approve "$run_id" 1
/opt/homebrew/bin/python3 -m supportactions approve "$run_id" 1
/opt/homebrew/bin/python3 -m supportactions replay "$run_id"
pending=$(/opt/homebrew/bin/python3 -m supportactions ask --account A "我的订单 A-101 退款 39 元")
printf '%s\n' "$pending"
run_id=$(printf '%s\n' "$pending" | awk 'NR == 1 {print $2}')
/opt/homebrew/bin/python3 -m supportactions reject "$run_id" 1
lookup=$(/opt/homebrew/bin/python3 -m supportactions ask --account B "查订单 B-202")
printf '%s\n' "$lookup"
run_id=$(printf '%s\n' "$lookup" | awk 'NR == 1 {print $2}')
/opt/homebrew/bin/python3 -m supportactions replay "$run_id"
pending=$(/opt/homebrew/bin/python3 -m supportactions ask --account A "A-103 退款 600 元")
printf '%s\n' "$pending"
run_id=$(printf '%s\n' "$pending" | awk 'NR == 1 {print $2}')
/opt/homebrew/bin/python3 -m supportactions approve "$run_id" 1 --approver reviewer-a
/opt/homebrew/bin/python3 -m supportactions approve "$run_id" 1 --approver reviewer-a
/opt/homebrew/bin/python3 -m supportactions approve "$run_id" 1 --approver reviewer-b
/opt/homebrew/bin/python3 -m supportactions demo --out .support-actions/demo.json
```

- `ask` 按固定规则生成计划，打印运行号和各步状态。退款提交后停在“待审”。查 B-202 会显示“可疑指令”。
- `approvals` 显示审批状态和截止时间。审批自入队起有效五分钟。`approve` 后面填运行号和步骤号，重复批准会返回已有结果。`--approver` 填演示审批人标识，省略时使用默认审批人。系统去掉首尾空格并忽略大小写，只保存用于去重的标识摘要。`reject` 会结束本次运行，余下步骤记为“未执行”。
- `replay` 按原顺序打印每条审计事件和最终状态，业务系统调用次数为零。记录保留可疑标记和大额审批计数。地址记录像 `青柚路***`，金额结果以分为单位。
- `demo` 使用独立的虚构数据和固定时钟。导出 JSON 的 `scenarios` 有七个 M1 场景，`m2_scenarios` 有六个 M2 场景，展示可疑备注、超限拒绝、边界重置和双人审批。重复生成的文件逐字节一致。

`.support-actions/state.sqlite3` 保存本地运行状态、审批和假业务数据，`.support-actions/traces/` 保存每次运行的 JSON Lines。新写入的计划和订单地址会遮住门牌号。业务写入和幂等结果在同一个 SQLite 事务中记录，退款入账时也会在该事务内核对剩余可退金额。幂等范围是同一运行号和同一步骤号，每次新的 `ask` 都会生成新的运行号。每次执行超时后都先按幂等键查结果，查不到才重试，最多重试两次。仍不明时停在“待人工核对”。

订单备注只作为工具结果数据。识别到“忽略之前的规则”“直接退款”“调用 refund”一类文字时，审计记录只保留标记。引用可疑备注作为参数的步骤会被拒绝。同一条结果中的结构化订单号可用于后续查单，仍需通过常规校验。每个账户默认每分钟可执行 10 次工具、每天可入账 2 笔退款。执行前核对限额，内部幂等查询和重试计入同一次工具调用。分钟和日期按 UTC 对齐，演示和测试用假时钟直接跨过边界。M1 数据库升级时根据已有审计记录恢复当前分钟调用数和当天入账退款数，再次打开只延续已有计数。

单笔超过 500 元时需要两位不同的审批人。恰好 500 元仍需一位审批人。Python 调用方可通过 `Runner` 的 `calls_per_minute`、`refunds_per_day` 和 `refund_cap_cents` 配置三个数值，金额上限以分为单位。

## 每个测试验证什么

| 测试 | 验证内容 |
| --- | --- |
| `test_addresses_are_redacted_in_every_persisted_record` | 改址成功和被拒时，计划、订单、审计事件、JSON Lines 和演示导出都遮住门牌号。 |
| `test_allow_list_blocks_entire_run` | 白名单外的工具会挡住整个计划，连排在它前面的查单也没有执行。 |
| `test_approved_refunds_cannot_exceed_remaining_amount` | 两笔事先提交的退款分别批准时，第二笔超出剩余可退额，退款表累计仍是实付金额。 |
| `test_backend_retries_same_effect_key_once_even_after_restart` | 直接用同一个键重试业务调用，重新打开数据库后继续重试，退款表仍只有一笔。 |
| `test_definite_backend_failure_does_not_retry` | 注入明确失败，执行器记录失败并停止，原地址保持原值。 |
| `test_expired_or_rejected_approval_never_executes` | 五分钟到期或明确拒绝后，即使再批准也没有退款。 |
| `test_foreign_order_blocks_dependent_step` | 查他人的订单和列表会被拒绝，依赖前一步的地址修改跳过，独立的本账户查询继续。 |
| `test_invalid_arguments_block_only_that_step_and_its_dependents` | 缺参数、类型错误和金额精度错误会被拦截，依赖步骤跳过，独立步骤继续。 |
| `test_invalid_run_id_is_rejected_before_storage` | 无效运行号在写入运行、审计事件和文件之前被拒绝。 |
| `test_overpayment_and_shipped_order_are_rejected` | 超过实付的退款和已发货订单改址都被拒绝。 |
| `test_refund_steps_keep_distinct_keys_and_exact_amounts_after_restart` | 同一次运行里的两笔部分退款分别入账。重启和重复批准后，步骤键、订单号和每笔分值仍然正确。 |
| `test_refund_waits_for_approval_and_runs_once_after_restart` | 独立命令行进程依次请求、批准和重复批准，最后重新打开数据库核对退款表。 |
| `test_rejected_approval_stops_remaining_independent_steps` | 人工拒绝退款后，独立改址和后续退款都跳过，地址和审批队列没有额外变化。 |
| `test_replay_matches_original_without_backend_access` | 回放和命令行输出逐条匹配执行时捕获的事件，覆盖时间、参数、审批和结果。任何假业务系统调用都会令测试失败。 |
| `test_second_timeout_is_confirmed_before_another_retry` | 首次超时查询为空，第二次写入成功后也超时。两次都用同一个键先查询，确认成功后停止。 |
| `test_timeout_after_effect_is_confirmed_by_key_without_retry` | 写入成功但响应超时时，通过幂等键确认成功，只发起一次执行。 |
| `test_trace_redacts_addresses_and_demo_is_deterministic` | 比较两次导出的字节，递归检查 POSIX 和 Windows 绝对路径，并检查门牌号、测试主机名、测试用户名和关键场景结果。 |
| `test_unknown_timeout_tries_twice_more_then_stops` | 初次执行和两次重试都不明时转人工核对，重复批准或重启均不会再自动重试。 |
| `test_unrecognized_order_and_account_numbers_are_redacted` | 误填的长订单号和账户号被拒绝，数据库和 JSON Lines 只留遮罩。 |
| `test_order_notes_are_flagged_in_replay_without_refund` | 三种指令单独出现时回放均有标记，普通送货备注无标记。四次查单都没有新增审批或退款。 |
| `test_step_using_flagged_tool_arguments_is_refused` | 从同一结果读取结构化订单号能查单，引用可疑备注的退款被拒且不入队。 |
| `test_m1_usage_survives_upgrade_and_reopen` | 旧库恢复当前限额，第 11 次调用和当天第 3 笔退款被拒，再次打开时计数不重复。 |
| `test_tool_limit_allows_exactly_ten_then_resets` | 第十次调用成功，第十一次在重启后仍被拒，其他账户可以调用，下一分钟恢复。 |
| `test_approved_refund_still_obeys_current_minute_limit` | 退款待审期间查单用完额度，即使批准退款也不会入账。 |
| `test_refund_limit_counts_two_effects_per_account_and_resets_at_midnight` | 两笔退款成功，超时后确认的退款只计一次，第三笔被拒，其他账户可用，跨日恢复。 |
| `test_configured_daily_refund_limit_survives_minute_change` | 日限额设为 1 笔后，同日跨分钟仍拒绝第二笔，跨午夜后可再次入账。 |
| `test_large_refund_requires_distinct_approvers_after_restart` | 超过 500 元时一人批准后待审，重启后同一审批人再批准仍待审，第二位批准后只入账一次。 |
| `test_approver_vote_deduplicates_without_storing_raw_identifier` | 路径形式的审批人标识只计一票，数据库不保存原文。 |
| `test_existing_approver_vote_is_preserved_when_hashed_on_upgrade` | 旧版已投的票升级后仍有效，数据库只保留标识摘要。 |
| `test_exact_cap_uses_one_approval_and_configured_cap_is_used` | 恰好 500 元只需一位审批人。更换金额上限后，新入队退款按新上限处理，已入队的审批人数要求在重启后继续生效。 |
| `test_export_shows_m2_scenarios` | 检查可疑备注和限额拒绝、分钟重置及双人审批，并核对同一订单跨日后实际退款的工具和入账金额。 |

## 未做

本仓库提供本地命令行工具，在线演示页单独维护。网络调用和多轮对话记忆均未接入。可疑文字识别只覆盖演示规则，审批人标识由本地命令提供，未接入身份认证。

## English overview

The command-line demo uses the Python standard library. A rule-based planner proposes actions. The runner checks tool access and account ownership, flags suspicious tool results, and enforces per-account limits. SQLite stores the simulated effects and idempotency keys. Audit events are saved in JSON Lines, and the demo command exports deterministic JSON.

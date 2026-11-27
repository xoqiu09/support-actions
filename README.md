# support-actions

青柚家电客服工具的本地演示。客服 Agent 要能查询订单、改地址和发起退款。这个项目演示怎样限制它能调用的工具、校验参数和账户权限、让人批准退款，并在执行超时或重复批准时防止重复退款。审计记录可以按运行号回放。

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
/opt/homebrew/bin/python3 -m supportactions demo --out .support-actions/demo.json
```

- `ask` 按固定规则生成计划，打印运行号和各步状态。示例先列出 A 的订单、查单和改地址，再提交退款。退款停在“待审”。
- `approvals` 显示审批状态和截止时间。审批自入队起有效五分钟。`approve` 后面填运行号和步骤号。对同一步重复批准会返回已有结果。`reject` 会结束本次运行，余下步骤记为“未执行”。
- `replay` 按原顺序打印每条审计事件和最终状态，业务系统调用次数为零。地址记录像 `青柚路***`，金额结果以分为单位。
- `demo` 用独立的虚构订单和固定时钟生成七个场景，写到指定文件。JSON 可供网站演示页读取，重复生成的文件逐字节一致。

`.support-actions/state.sqlite3` 保存本地运行状态、审批和假业务数据，`.support-actions/traces/` 保存每次运行的 JSON Lines。新写入的计划和订单地址会遮住门牌号。业务写入和幂等结果在同一个 SQLite 事务中记录，退款入账时也会在该事务内核对剩余可退金额。幂等范围是同一运行号和同一步骤号，每次新的 `ask` 都会生成新的运行号。每次执行超时后都先按幂等键查结果，查不到才重试，最多重试两次。仍不明时停在“待人工核对”。

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

## M1 未做

真实模型、网络调用、网页和多轮对话记忆均未接入。订单和业务结果均为虚构数据。

## English overview

A standard-library Python demo of customer-support tool calls. A rule-based planner proposes actions, and the runner enforces a tool allow-list, argument checks, account ownership, and human approval for refunds. SQLite stores the simulated effects and idempotency keys. JSON Lines support audit replay without business calls, and the demo command exports deterministic JSON.

# Instruction queue

Find a local chat using the native picker or `carrie-on chats list`. Add an instruction with `queue add CHAT`; inspect it with `queue list` or `queue show UUID`. Edit unclaimed instructions with `queue edit UUID`, cancel with `queue cancel UUID`, and inspect outcomes with `queue list --history` and `queue deliveries UUID`. There is no pause setting. Stopping the app stops dispatch and preserves the queue.

Enrollment captures intent and timing anchors. Readiness is checked afresh by the server when claiming an item.

| Captured at enrollment | Checked before delivery |
| --- | --- |
| Chat UUID, title, exact instruction, queue item UUID | Chat is in the unarchived local catalog |
| Latest task message ID and model | Task and model still match, or the outgoing message is already observed |
| Whether the task was running | Work is idle and has not finished or stopped for an unrelated reason |
| Account, quota bucket, relevant reset timestamps | Same account/bucket; recorded windows advanced to future times; every window has headroom |
| No approval or goal-readiness decision | No pending approval/input or paused, blocked, or budget-limited Codex goal |

An early manual reset can make work eligible before its original deadline. Headroom alone, unchanged timestamps, missing windows, or a changed account/bucket cannot authorize delivery. Idle work deliberately queued by the user may receive an instruction after reset even if its preceding turn completed. Work enrolled while busy is marked changed if it finishes normally or stops for a reason other than quota exhaustion.

The server reuses successful, ordered usage observations up to 60 seconds old for enrollment while their reset timestamps remain future. Failed reads invalidate this cache. Ambiguous or expired observations require a fresh read. Dispatch never uses cached usage. Dormant chats are checked against the catalog before opening their deep link. Read-only helpers cannot start a model turn.

Slow enrollment and dispatch do not hold database locks. Queue membership and original creation input are rechecked in the final transaction. Incomplete anchors never become queue items. The native client keeps draft text, and both clients can retry creation with the same UUID without making a second item.

States are queued, sent, changed, cancelled, and failed. Temporary connection or send-reply failures keep an item queued with an error and increment its deferrals. Changed work requires inspection and a new enrollment. Delivery attempts retain their own outcomes and leases. Expired attempts are reclaimed automatically; fresh Codex observations suppress resending an already-observed message. Accepted delivery means the desktop accepted the instruction, not that the work completed.

Queued cards remain amber, active sends show a blue spinner, and failed/changed work has a red warning with its reason. Active delivery disables edits/removal only for that item. A failed item can be dismissed, retaining history. An expired lease stops the spinner. Uncertain client writes are reconciled against the server before enabling another action.

See the [architecture and API](../ARCHITECTURE.md), [usage guide](../USAGE.md), and [verification matrix](verification.md). Automated tests cover real SQLite transactions, process death, concurrent claims, expiry, stale workers, replay, quota semantics, protocol boundaries, HTTP, CLI, and native interaction races. Real dispatch requires an explicitly authorized disposable chat.

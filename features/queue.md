# Instruction queue

Find a local chat using the native picker or `carrie-on chats list`. Add an instruction with `queue add CHAT`; inspect it with `queue list` or `queue show UUID`. Edit unclaimed instructions with `queue edit UUID`, cancel with `queue cancel UUID`, and inspect outcomes with `queue list --history`. There is no pause setting. Stopping the app stops dispatch and preserves the queue.

Enrollment captures intent and timing anchors. Readiness is checked afresh by the server when claiming an item.

| Captured at enrollment | Checked before delivery |
| --- | --- |
| Chat UUID, title, exact instruction, queue item UUID | Chat is in the unarchived local catalog |
| Latest task message ID and model | Task and model still match, or the outgoing message is already observed |
| Whether the task was running | Work is idle and has not finished or stopped for an unrelated reason |
| Account, quota bucket, relevant reset timestamps | Same account/bucket; recorded windows advanced to future times; every window has headroom |
| No approval or goal-readiness decision | No pending approval/input or paused, blocked, or budget-limited Codex goal |

An early manual reset can make work eligible before its original deadline. Headroom alone, unchanged timestamps, missing windows, or a changed account/bucket cannot authorize delivery. Idle work deliberately queued by the user may receive an instruction after reset even if its preceding turn completed. Work enrolled while busy becomes stale if it finishes normally; unrelated stops become retryable failures requiring fresh checks.

The server reuses successful, ordered usage observations up to 60 seconds old for enrollment while their reset timestamps remain future. Failed reads invalidate this cache. Ambiguous or expired observations require a fresh read. Dispatch never uses cached usage. Dormant chats are checked against the catalog before opening their deep link. Read-only helpers cannot start a model turn.

Slow enrollment and dispatch do not hold database locks. Queue membership and original creation input are rechecked in the final transaction. Incomplete anchors never become queue items. The native client keeps draft text, and both clients can retry creation with the same UUID without making a second item.

States are queued, failed, stale, cancelled, and sent. Failed entries retain an error and retry after their immutable delivery attempt expires. Stale and cancelled entries never retry. Sent means the desktop accepted the instruction, not that the work completed. Attempts are private ownership records, with no public read or update API. Queue state changes do not alter them. There are no deferrals or server-side check timestamps.

Queued cards are amber; active dispatch shows a blue spinner. Failed entries and stale conversations show red warnings with retry or terminal guidance. Active ownership blocks editing/removal only for that entry, including a failed entry awaiting expiry. Idle failed entries remain editable. Dismissing a stale warning retains history. Uncertain client writes are reconciled before another action is enabled.

See the [architecture and API](../ARCHITECTURE.md), [usage guide](../USAGE.md), and [verification matrix](verification.md). Automated tests cover real SQLite transactions, process death, concurrent claims, expiry, replay, quota semantics, protocol boundaries, HTTP, CLI, and native interaction races. Real dispatch requires an explicitly authorized disposable chat.

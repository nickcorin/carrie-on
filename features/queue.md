# Instruction queue

Find a local chat using the native picker or `carrie-on chats list`. Add an instruction with `queue add CHAT`; inspect it with `queue list` or `queue show UUID`. Edit unclaimed instructions with `queue edit UUID`, cancel with `queue cancel UUID`, and inspect outcomes with `queue list --history`. There is no pause setting. Stopping the app stops dispatch and preserves the queue.

Enrollment captures intent and timing anchors. Readiness is checked afresh by the server when claiming an item.

| Captured at enrollment | Checked before delivery |
| --- | --- |
| Chat UUID, title, exact instruction, queue item UUID | Chat is in the unarchived local catalog |
| Latest task message ID and model | Task and model still match, or the outgoing message is already observed |
| Whether the task was running | Work is idle; work queued while running has not completed normally |
| Account, quota bucket, each named window's reset timestamp | Same account/bucket; a saved window's timestamp differs; quota is currently usable |
| No approval or goal-readiness decision | No pending approval/input or paused, blocked, or budget-limited Codex goal |

A quota window is identified by its `resetsAt` timestamp within the account, quota bucket, and window name. Work becomes eligible when any captured window has a different timestamp and quota is available. Every currently reported window must have headroom, and Codex must report usage allowed. Percentage decreases, elapsed deadlines, and newly reported windows do not count as a changed captured window. Exhaustion need not be observed, and an earlier timestamp also identifies a different window. A changed window can qualify before the original deadline. Idle work deliberately queued by the user may receive an instruction even if its preceding turn completed. Work enrolled while busy becomes stale if it finishes normally; failed or interrupted work can resume after a window change without a quota-exhaustion error.

Enrollment and dispatch read fresh usage. An earlier status reading cannot make a reset before enrollment count toward a new entry. Ambiguous quota buckets require explicit selection. Dormant chats are checked against the catalog before opening their deep link. Read-only helpers cannot start a model turn.

Slow enrollment and dispatch do not hold database locks. Storage enforces unique entry identities and pending chat membership. Incomplete anchors never become queue items. The native client keeps draft text, and both clients can retry creation with the same UUID without making a second item. The HTTP boundary returns that entry's current state; retries never overwrite edits or reactivate terminal entries.

States are queued, failed, stale, cancelled, and sent. Failed entries retain an error and retry after their immutable delivery attempt expires. Stale and cancelled entries never retry. Sent means the desktop accepted the instruction, not that the work completed. Attempts are private ownership records, with no public read or update API. Queue state changes do not alter them. There are no deferrals or server-side check timestamps.

Queued cards are amber; active dispatch shows a blue spinner. Failed entries and stale conversations show red warnings with retry or terminal guidance. Active ownership blocks editing/removal only for that entry, including a failed entry awaiting expiry. Idle failed entries remain editable. Dismissing a stale warning retains history. Uncertain client writes are reconciled before another action is enabled.

See the [architecture and API](../ARCHITECTURE.md), [usage guide](../USAGE.md), and [verification matrix](verification.md). Automated tests cover real SQLite transactions, process death, concurrent claims, expiry, replay, quota semantics, protocol boundaries, HTTP, CLI, and native interaction races. Real dispatch requires an explicitly authorized disposable chat.

## Live dispatch acceptance

Use Carrie On Dev and a disposable Codex chat that you have permission to send to. Keep Codex open and the Mac awake. Do not enroll the same chat through the release app during this check.

1. Queue a distinctive instruction through the menu or CLI. Record its item UUID and confirm that the stored instruction matches exactly.
2. Before a captured quota window changes, confirm that no instruction arrives. A failed entry with a quota error remains eligible for retry.
3. Wait for a captured window's reset timestamp to change while quota is available. Leave the chat idle with the same message and model. Allow the current delivery lease to expire and the worker to run again. Confirm that Codex receives the exact instruction in that chat and the entry becomes `sent`.
4. Quit and reopen Dev. Confirm that the entry remains sent in history and that subsequent worker checks do not send it again.
5. With a separate pending entry, continue its chat manually before the reset. Confirm that Carrie On marks the entry `stale` and does not send its queued instruction.

If no quota window changes during the check, record reset delivery as unverified. Automated tests cover simulated resets and transport failures; they do not establish a live send against the installed Codex version.

# Continuation queue

Find a chat with `python3 carrie_on.py threads SEARCH`, enroll it with `queue CHAT`, and inspect it with `list`. `remove CHAT` cancels enrollment; `pause` and `resume` control dispatch globally. `install` registers the macOS worker; `uninstall` removes it without deleting history.

Enrollment records intent and its timing anchors. Readiness is evaluated when a continuation is due.

Every worker check reads fresh usage for waiting enrollments, including before their recorded deadline. An early manual reset is eligible as soon as all recorded windows have advanced reset timestamps still in the future and every reported window has headroom. The old deadline need not elapse. Headroom alone, unchanged timestamps, missing windows, or a different account/bucket cannot authorize continuation. Existing enrollments use the same rule without migration; chat and delivery safeguards still apply.

While sending an enrollment or confirming its delivery, the worker saves optional `processing_until` presentation metadata: a Unix timestamp in seconds, expiring after 120 seconds. It clears the field after each check and before recording history. Missing, null, or expired values mean no active processing indicator. This field never authorizes dispatch or retries. Status reads use the atomically replaced queue snapshot without acquiring the mutation lock, so processing remains visible during network operations. All mutations still require the queue lock. A crash can leave an indicator until its expiry; an unconfirmed sending entry then displays a warning rather than continued animation.

Worker errors during reactivation checks persist as `reactivation_error` on the enrollment. This is presentation metadata, separate from the waiting/sending/needs-attention dispatch state: transient failures may retry, and uncertain sends still cannot be blindly resent. A successful subsequent check clears the error; ordinary waits do not count as failures. Existing files without this optional field remain readable, and held `needs_attention` enrollments retain their failed appearance. Connection setup failures mark waiting and sending enrollments; dry runs never write failure state.

| Captured at enrollment | Checked fresh at resume |
| --- | --- |
| Chat ID, title, exact instruction, enrollment ID | Chat remains in the unarchived local catalog |
| Latest Codex turn ID and model | Task and model still match the recorded anchors |
| Whether the turn has persisted completion evidence | Turn has a completion timestamp; running work did not already finish or stop for another reason |
| Account, usage bucket, relevant reset timestamps | Same account/bucket, advanced reset timestamps, available quota in every window |
| No approval or goal-readiness decision | No pending approval/input or paused, blocked, or budget-limited goal |

The local interface reuses a validated usage observation from its ordinary background reads when it is at most 60 seconds old and none of its reset timestamps has elapsed. Failed reads invalidate the cache; out-of-order responses cannot replace a newer observation. Missing, expired, or ambiguous observations fall back to a fresh quota read. Cached usage never authorizes dispatch. No cache or queue-file migration is required; existing enrollments retain their anchors and safeguards.

Enrollment uses the pinned Python SDK to read thread metadata with `includeTurns: false` and the latest turn with `limit: 1, itemsView: notLoaded`. The desktop supplies current model settings. Cached quota avoids a second usage fetch, but the SDK still starts a read-only helper for the turn anchor. A dormant chat is checked before opening to avoid reopening archived work. Enrollment does not read goal state; dispatch checks it after quota becomes available.

The helper does not own desktop chats. Its runtime status is not evidence of desktop idleness. Codex can label an unfinished stored turn `interrupted`. Carrie On requires a positive `completedAt` timestamp and a terminal status before dispatch. Missing completion evidence means waiting, including while approvals or input are pending. New enrollments request no turn items, tool output, or attachments. Standard thread metadata can include a short chat preview. Model selection and continuation dispatch remain with the desktop owner.

SDK responses are limited to 1 MiB before JSON decoding, with a 30-second request deadline. A limit or timeout closes the helper rather than authorizing a continuation. The SDK version is pinned because the bounded transport extends its reader and request lifecycle. The desktop retains its existing 32 MiB frame circuit breaker.

Entries written before turn-ID enrollment retain their original message-ID anchors. Before their first eligible dispatch, a bounded desktop snapshot maps the unchanged message to its accepted turn ID, which is saved before further checks. This compatibility read retains the desktop limit and can still fail for an oversized legacy chat; it never silently re-anchors changed work. New entries do not request snapshots.

Before sending, the worker saves `sending` state. A desktop acceptance receipt is saved as `accepted_turn_id` before finishing the entry, so restart can finish bookkeeping without resending. If the acknowledgement is lost before that receipt is saved, the entry requires review. The compact SDK responses do not expose the client message identifier needed to prove delivery; a later turn is not assumed to be ours. Legacy sends retain their original bounded reconciliation path.

Codex reads during enrollment happen outside the queue lock. The final save reloads the queue, rechecks duplicate membership, and preserves concurrent changes such as pause or removal. Enqueue responses still confirm a durable enrollment; they are not optimistic placeholders. If the task anchor or reset baseline cannot be obtained, enrollment fails without saving an incomplete entry.

`python3 -m unittest -v` checks reset gating, busy and changed chats, account mismatches, delivery recovery, single-use removal, cached enrollment and expiry, deferred readiness, concurrent duplicate rejection, and queue access during slow enrollment reads. Socket tests verify that dormant chats are validated before opening. `doctor --thread UUID` checks live read compatibility without inference. Real dispatch must be tested only against an explicitly authorized disposable chat. Keep synthetic test state separate from `resume-queue.json`.

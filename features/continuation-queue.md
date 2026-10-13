# Continuation queue

Find a chat with `python3 tokenmaxxer.py threads SEARCH`, enroll it with `queue CHAT`, and inspect it with `list`. `remove CHAT` cancels enrollment; `pause` and `resume` control dispatch globally. `install` registers the macOS worker; `uninstall` removes it without deleting history.

Enrollment records intent and its timing anchors. Readiness is evaluated when a continuation is due.

Worker errors during due reactivation persist as `reactivation_error` on the enrollment. This is presentation metadata, separate from the waiting/sending/needs-attention dispatch state: transient failures may retry, and uncertain sends still cannot be blindly resent. A successful subsequent check clears the error; ordinary waits do not count as failures. Existing files without this optional field remain readable, and held `needs_attention` enrollments retain their failed appearance. Connection setup failures mark only due enrollments; dry runs never write failure state.

| Captured at enrollment | Checked fresh at resume |
| --- | --- |
| Chat ID, title, exact instruction, enrollment ID | Chat remains in the unarchived local catalog |
| Latest task message ID and model | Task and model still match the recorded anchors |
| Whether the task was running | Task is idle; running work did not already finish or stop for another reason |
| Account, usage bucket, relevant reset timestamps | Same account/bucket, advanced reset timestamps, available quota in every window |
| No approval or goal-readiness decision | No pending approval/input or paused, blocked, or budget-limited goal |

The local interface reuses a validated usage observation from its ordinary background reads when it is at most 60 seconds old and none of its reset timestamps has elapsed. Failed reads invalidate the cache; out-of-order responses cannot replace a newer observation. Missing, expired, or ambiguous observations fall back to a fresh quota read. Cached usage never authorizes dispatch. No cache or queue-file migration is required; existing enrollments retain their anchors and safeguards.

Enqueueing a loaded chat by ID reads its live task anchor without starting an app-server helper when recent quota is available. Title lookup and opening a dormant chat still require catalog reads; a dormant chat is checked before opening to avoid reopening archived work. A chat awaiting approval or with a paused goal can be queued, but will not resume until the blocking condition is resolved. If its task changes meanwhile, the enrollment requires review.

Codex reads during enrollment happen outside the queue lock. The final save reloads the queue, rechecks duplicate membership, and preserves concurrent changes such as pause or removal. Enqueue responses still confirm a durable enrollment; they are not optimistic placeholders. If the task anchor or reset baseline cannot be obtained, enrollment fails without saving an incomplete entry.

`python3 -m unittest -v` checks reset gating, busy and changed chats, account mismatches, delivery recovery, single-use removal, cached enrollment and expiry, deferred readiness, concurrent duplicate rejection, and queue access during slow enrollment reads. Socket tests verify that dormant chats are validated before opening. `doctor --thread UUID` checks live read compatibility without inference. Real dispatch must be tested only against an explicitly authorized disposable chat. Keep synthetic test state separate from `resume-queue.json`.

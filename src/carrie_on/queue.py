"""Track queued instructions and decide when Codex can receive them."""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal
from uuid import UUID

from carrie_on.codex.models import Chat, Usage

DEFAULT_INSTRUCTION = "Please continue with your last task."
EntryState = Literal["queued", "failed", "stale", "cancelled", "sent"]
PENDING_STATES: tuple[EntryState, ...] = ("queued", "failed")


class QueueError(Exception):
    """Reject a queue operation before it writes, with a code callers can handle."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class QueueEntry:
    """Store a queued instruction and the chat state recorded when it was added.

    Storage sets dispatching from the entry's unexpired delivery attempts when it
    reads the entry. The queue does not store a dispatching state. Reset timestamps
    identify the saved quota windows within this account and bucket. Datetime
    fields use timezone-aware UTC values.
    """

    id: UUID
    chat_id: UUID
    title: str
    account_id: str
    limit_id: str
    anchor_message_id: str | None
    model: str
    instructions: str
    reset_windows: dict[str, int]
    enrolled_while_busy: bool
    queued_at: datetime
    updated_at: datetime
    anchor_turn_id: str | None = None
    state: EntryState = "queued"
    error: str = ""
    revision: int = 1
    finished_at: datetime | None = None
    dispatching: bool = False

    @classmethod
    def enroll(
        cls,
        entry_id: UUID,
        instructions: str,
        chat: Chat,
        usage: Usage,
        now: datetime,
    ) -> "QueueEntry":
        """Record the current turn and each quota window's reset timestamp."""
        if not (chat.message_id or chat.turn_id):
            raise QueueError("invalid_chat", "Chat has no identifiable conversation anchor")
        return cls(
            id=entry_id,
            chat_id=UUID(chat.thread_id),
            title=chat.title,
            account_id=usage.account_id,
            limit_id=usage.limit_id,
            anchor_message_id=chat.message_id,
            anchor_turn_id=chat.turn_id,
            model=chat.model,
            instructions=instructions,
            reset_windows={window.name: window.reset_at for window in usage.windows},
            enrolled_while_busy=chat.busy,
            queued_at=now,
            updated_at=now,
        )

    def update_instructions(self, instructions: str, now: datetime) -> "QueueEntry":
        """Replace instructions on a pending entry that has no active delivery."""
        if self.dispatching:
            raise QueueError(
                "delivery_active", "A delivery is active; wait for its lease to expire"
            )
        if self.state not in PENDING_STATES:
            raise QueueError("item_finished", "Only pending instructions can be edited")
        return replace(self, instructions=instructions, updated_at=now)

    def update_state(self, state: EntryState, now: datetime, *, error: str = "") -> "QueueEntry":
        """Change a pending entry's state and set its error and completion time.

        Failed entries require an error and remain eligible for retry. Finished
        entries cannot return to the queue. A stale entry can still be cancelled.
        """
        if state == "failed" and not error.strip():
            raise QueueError("missing_error", "A failed entry requires an error")
        error = error if state in ("failed", "cancelled") else ""
        if self.state == state and self.error == error:
            return self
        if self.state not in PENDING_STATES and not (
            self.state == "stale" and state == "cancelled"
        ):
            raise QueueError("item_finished", "This entry has already finished")
        if state == "cancelled" and self.dispatching:
            raise QueueError(
                "delivery_active", "A delivery is active; wait for its lease to expire"
            )
        return replace(
            self,
            state=state,
            error=error,
            updated_at=now,
            finished_at=None if state in PENDING_STATES else now,
            dispatching=self.dispatching and state in PENDING_STATES,
        )

    def reset_ready(self, usage: Usage) -> bool:
        """Check for a changed reset timestamp and available quota in the saved account and bucket."""
        if usage.account_id != self.account_id or usage.limit_id != self.limit_id:
            raise QueueError("quota_changed", "Codex account or quota bucket changed")
        if not usage.allowed or any(window.used >= 100 for window in usage.windows):
            return False
        return any(
            window.reset_at != self.reset_windows[window.name]
            for window in usage.windows
            if window.name in self.reset_windows
        )

    def matches_anchor(self, chat: Chat) -> bool:
        """Check that the chat still has the saved turn and opening message IDs."""
        return (self.anchor_turn_id is None or chat.turn_id == self.anchor_turn_id) and (
            self.anchor_message_id is None or chat.message_id == self.anchor_message_id
        )

    def assess(self, usage: Usage, chat: Chat | None) -> "Eligibility":
        """Check whether Codex accepted the message or the entry is ready to send."""
        if chat is None:
            return Eligibility(
                "failed", "Chat is archived, renamed, or absent from the local catalog"
            )
        if chat.thread_id != str(self.chat_id):
            raise QueueError("chat_mismatch", "Desktop returned a different chat")
        if str(self.id) in chat.seen_message_ids:
            return Eligibility("sent", "Confirmed the previously accepted instruction")
        if not self.matches_anchor(chat):
            return Eligibility("stale", "Conversation continued after enrollment")
        if chat.model != self.model:
            return Eligibility("failed", "Chat model changed after enrollment")
        if not self.reset_ready(usage):
            return Eligibility("failed", "Waiting for a different quota window and available quota")
        if chat.busy or chat.blocked:
            return Eligibility("failed", "Chat is active or requires attention")
        if self.enrolled_while_busy and chat.status == "completed":
            return Eligibility("stale", "Work finished before the usage reset")
        return Eligibility("ready", "Quota window changed and quota is available")


@dataclass(frozen=True, kw_only=True)
class DeliveryAttempt:
    """Give the creating worker exclusive access to an entry until the attempt expires."""

    id: UUID
    entry: QueueEntry
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class Eligibility:
    """Describe whether an entry can be sent and why."""

    outcome: Literal["ready", "sent", "stale", "failed"]
    reason: str


@dataclass(frozen=True, kw_only=True)
class QueueSummary:
    """Count entries by state and active deliveries from the same storage read."""

    counts: dict[EntryState, int]
    dispatching: int

    @property
    def count(self) -> int:
        return sum(self.counts.get(state, 0) for state in PENDING_STATES)

    @property
    def needs_attention(self) -> int:
        return self.counts.get("failed", 0) + self.counts.get("stale", 0)

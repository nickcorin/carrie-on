"""Queue entries, delivery attempts, and decisions over typed Codex observations."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal
from uuid import UUID

from carrie_on.codex.models import Chat, Usage

DEFAULT_INSTRUCTION = "Please continue with your last task."
EntryState = Literal["queued", "failed", "stale", "cancelled", "sent"]
PENDING_STATES: tuple[EntryState, ...] = ("queued", "failed")


class QueueError(Exception):
    """An expected rejection, identified by a stable code; no write was committed."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class QueueEntry:
    """Captured intent and current state, with timezone-aware UTC timestamps.

    Original instructions and the requested bucket preserve creation identity
    across edits and automatic bucket selection. Dispatching is a read snapshot
    derived from active ownership rather than a persisted entry state.
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
    original_instructions: str = field(repr=False)
    anchor_turn_id: str | None = None
    requested_limit_id: str | None = field(default=None, repr=False)
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
        *,
        limit_id: str | None = None,
    ) -> "QueueEntry":
        """Capture the current turn and exhausted or soonest future reset windows."""
        if not (chat.message_id or chat.turn_id):
            raise QueueError("invalid_chat", "Chat has no identifiable conversation anchor")
        exhausted = [window for window in usage.windows if window.used >= 100]
        future = [window for window in usage.windows if window.reset_at > now.timestamp()]
        if not exhausted and not future:
            raise QueueError(
                "stale_usage", "Usage timestamps are stale; refresh Codex and try again"
            )
        selected = exhausted or [min(future, key=lambda window: window.reset_at)]
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
            original_instructions=instructions,
            requested_limit_id=limit_id,
            reset_windows={window.name: window.reset_at for window in selected},
            enrolled_while_busy=chat.busy,
            queued_at=now,
            updated_at=now,
        )

    def require_creation_input(
        self, chat_id: UUID, instructions: str, limit_id: str | None
    ) -> None:
        """Reject reuse of this entry's UUID for different original caller input."""
        if (chat_id, instructions, limit_id) != (
            self.chat_id,
            self.original_instructions,
            self.requested_limit_id,
        ):
            raise QueueError(
                "id_reused", "This UUID was already used with different creation input"
            )

    def update_instructions(self, instructions: str, now: datetime) -> "QueueEntry":
        """Replace the current instructions while retaining the original creation input."""
        if self.dispatching:
            raise QueueError(
                "delivery_active", "A delivery is active; wait for its lease to expire"
            )
        if self.state not in PENDING_STATES:
            raise QueueError("item_finished", "Only pending instructions can be edited")
        return replace(self, instructions=instructions, updated_at=now)

    def update_state(self, state: EntryState, now: datetime, *, error: str = "") -> "QueueEntry":
        """Apply a lifecycle transition; failed work retries, terminal work stays terminal."""
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

    def reset_ready(self, usage: Usage, now: datetime) -> bool:
        """Require the captured account/bucket, fresh reset windows, and headroom."""
        if usage.account_id != self.account_id or usage.limit_id != self.limit_id:
            raise QueueError("quota_changed", "Codex account or quota bucket changed")
        if not usage.allowed or any(window.used >= 100 for window in usage.windows):
            return False
        windows = {window.name: window.reset_at for window in usage.windows}
        return all(
            name in windows and windows[name] > max(baseline, now.timestamp())
            for name, baseline in self.reset_windows.items()
        )

    def matches_anchor(self, chat: Chat) -> bool:
        """Compare every conversation identity captured at enrollment."""
        return (self.anchor_turn_id is None or chat.turn_id == self.anchor_turn_id) and (
            self.anchor_message_id is None or chat.message_id == self.anchor_message_id
        )

    def assess(self, usage: Usage, chat: Chat | None, now: datetime) -> "Eligibility":
        """Decide readiness from fresh observations, reconciling accepted sends first."""
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
        if not self.reset_ready(usage, now):
            return Eligibility("failed", "Waiting for fresh reset confirmation and available quota")
        if chat.busy or chat.blocked:
            return Eligibility("failed", "Chat is active or requires attention")
        if self.enrolled_while_busy and chat.status == "completed":
            return Eligibility("stale", "Work finished before the usage reset")
        if self.enrolled_while_busy and not chat.quota_failed:
            return Eligibility("failed", "Chat stopped for a reason other than quota exhaustion")
        return Eligibility("ready", "Ready to send after a fresh reset")


@dataclass(frozen=True, kw_only=True)
class DeliveryAttempt:
    """Immutable ownership until expiry, returned only to the worker that acquires it."""

    id: UUID
    entry: QueueEntry
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class Eligibility:
    """Readiness decision without storage or transport effects."""

    outcome: Literal["ready", "sent", "stale", "failed"]
    reason: str


@dataclass(frozen=True, kw_only=True)
class QueueSummary:
    """State counts and current dispatch activity from the same storage snapshot."""

    counts: dict[EntryState, int]
    dispatching: int

    @property
    def count(self) -> int:
        return sum(self.counts.get(state, 0) for state in PENDING_STATES)

    @property
    def needs_attention(self) -> int:
        return self.counts.get("failed", 0) + self.counts.get("stale", 0)

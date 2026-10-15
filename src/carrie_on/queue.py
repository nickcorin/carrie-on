"""Queue intent and pure eligibility policy, independent of transport and storage."""

from dataclasses import dataclass
from typing import Literal

from carrie_on.codex.models import Chat, Usage

DEFAULT_INSTRUCTION = "Please continue with your last task."
ItemState = Literal["queued", "sent", "changed", "cancelled", "failed"]
CompletionOutcome = Literal["sent", "changed", "deferred", "failed"]
DeliveryOutcome = Literal["sent", "changed", "deferred", "failed", "expired"]
OPEN_STATES = ("queued", "changed", "failed")


class QueueError(Exception):
    """An expected rejection; code is stable and no operation was committed."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class EnqueueCommand:
    """Original creation identity, retained unchanged for retries after edits.

    IDs are canonical UUID strings. The instruction is exact user text, not
    stripped. A missing limit_id means automatic selection at enrollment; it
    remains missing in the creation identity even after a bucket is selected.
    """

    id: str
    chat_id: str
    instruction: str
    limit_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class QueueItem:
    """Durable enrollment and its lifecycle; timestamps are Unix seconds."""

    id: str
    chat_id: str
    title: str
    account_id: str
    limit_id: str
    anchor_message_id: str
    model: str
    instruction: str
    reset_windows: dict[str, int]
    enrolled_while_busy: bool
    queued_at: float
    updated_at: float
    state: ItemState = "queued"
    reason: str = "Waiting for the recorded usage reset"
    deferrals: int = 0
    revision: int = 1
    error: str | None = None
    finished_at: float | None = None

    @classmethod
    def enroll(cls, command: EnqueueCommand, chat: Chat, usage: Usage, now: float) -> "QueueItem":
        """Capture the latest turn and exhausted (or soonest future) windows."""
        exhausted = [window for window in usage.windows if window.used >= 100]
        future = [window for window in usage.windows if window.reset_at > now]
        if not exhausted and not future:
            raise QueueError(
                "stale_usage", "Usage timestamps are stale; try again after Codex refreshes them"
            )
        selected = exhausted or [min(future, key=lambda window: window.reset_at)]
        return cls(
            id=command.id,
            chat_id=chat.thread_id,
            title=chat.title,
            account_id=usage.account_id,
            limit_id=usage.limit_id,
            anchor_message_id=chat.message_id,
            model=chat.model,
            instruction=command.instruction,
            reset_windows={window.name: window.reset_at for window in selected},
            enrolled_while_busy=chat.busy,
            queued_at=now,
            updated_at=now,
        )

    def reset_ready(self, usage: Usage, now: float) -> bool:
        """Require the enrolled account/bucket, fresh resets, and all-window headroom."""
        if usage.account_id != self.account_id or usage.limit_id != self.limit_id:
            raise QueueError("quota_changed", "Codex account or quota bucket changed")
        if not usage.allowed or any(window.used >= 100 for window in usage.windows):
            return False
        windows = {window.name: window for window in usage.windows}
        return all(
            name in windows and windows[name].reset_at > max(baseline, now)
            for name, baseline in self.reset_windows.items()
        )


@dataclass(frozen=True, kw_only=True)
class ActiveDelivery:
    """Unfinished attempt; its lease may already have expired."""

    id: str
    lease_expires_at: float
    sending_at: float | None = None


@dataclass(frozen=True, kw_only=True)
class QueueEntry:
    """Read projection grouping an enrollment with its optional active attempt."""

    item: QueueItem
    active_delivery: ActiveDelivery | None = None


@dataclass(frozen=True, kw_only=True)
class Delivery:
    """An attempt with exclusive ownership until lease_expires_at (Unix seconds)."""

    id: str
    queue_item_id: str
    instruction: str
    started_at: float
    lease_expires_at: float
    sending_at: float | None = None
    finished_at: float | None = None
    outcome: DeliveryOutcome | None = None
    reason: str | None = None
    receipt: str | None = None


@dataclass(frozen=True)
class Eligibility:
    """A pure decision from fresh observations; ready permits a fenced send."""

    outcome: Literal["ready", "sent", "changed", "deferred"]
    reason: str


def evaluate(item: QueueItem, usage: Usage, chat: Chat | None, now: float) -> Eligibility:
    """Evaluate fresh observations without I/O; absent catalog entries cannot send."""
    if chat is None:
        return Eligibility("changed", "Chat is archived, renamed, or absent from the local catalog")
    if chat.thread_id != item.chat_id:
        raise QueueError("chat_mismatch", "Desktop returned a different chat")
    if item.id in chat.seen_message_ids:
        return Eligibility("sent", "Confirmed the previously accepted instruction")
    if chat.message_id != item.anchor_message_id or chat.model != item.model:
        return Eligibility(
            "changed", "Chat changed after enrollment; inspect it before re-queueing"
        )
    if not item.reset_ready(usage, now):
        return Eligibility("deferred", "Waiting for fresh reset confirmation and available quota")
    if chat.busy or chat.blocked:
        return Eligibility("deferred", "Waiting: chat is active or requires attention")
    if item.enrolled_while_busy and chat.status == "completed":
        return Eligibility("changed", "Work finished before the usage reset")
    if item.enrolled_while_busy and not chat.quota_failed:
        return Eligibility("changed", "Chat stopped for a reason other than quota exhaustion")
    return Eligibility("ready", "Ready to send after a fresh reset")

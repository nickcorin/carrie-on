"""Decode and reconcile retired JSON queues into current, portable import records."""

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

from carrie_on.queue import Delivery, EnqueueCommand, ItemState, QueueItem


@dataclass(frozen=True, kw_only=True)
class LegacyEntry:
    """Validated enrollment from the retired version 2 queue format."""

    id: str
    thread_id: str
    title: str
    account_id: str
    limit_id: str
    anchor_message_id: str
    model: str
    prompt: str
    reset_windows: dict[str, int]
    enrolled_while_busy: bool
    state: Literal["waiting", "sending", "needs_attention"] = "waiting"
    note: str = "Waiting for the recorded usage reset"
    attempted_at: float | None = None
    reactivation_error: str | None = None
    processing_until: float | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "LegacyEntry":
        """Validate saved enrollment fields without rejecting previously accepted text."""
        entry = cls(**raw)
        if not isinstance(entry.id, str):
            raise TypeError("Invalid queue entry ID")
        UUID(entry.id)
        for value in (
            entry.thread_id,
            entry.title,
            entry.account_id,
            entry.limit_id,
            entry.anchor_message_id,
            entry.model,
            entry.prompt,
            entry.note,
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Invalid queue entry text field")
        if entry.state not in {"waiting", "sending", "needs_attention"}:
            raise ValueError("Invalid queue entry state")
        for timestamp in (entry.attempted_at, entry.processing_until):
            if timestamp is not None and (
                type(timestamp) not in (int, float) or not math.isfinite(timestamp)
            ):
                raise ValueError("Invalid legacy attempt timestamp")
        if entry.state == "sending" and entry.attempted_at is None:
            raise ValueError("Sending entry has no valid attempt timestamp")
        if (
            not isinstance(entry.reset_windows, dict)
            or not entry.reset_windows
            or any(
                key not in {"primary", "secondary"} or type(value) is not int or value <= 0
                for key, value in entry.reset_windows.items()
            )
        ):
            raise ValueError("Invalid reset windows")
        if type(entry.enrolled_while_busy) is not bool:
            raise ValueError("Invalid enrollment state")
        if entry.reactivation_error is not None and (
            not isinstance(entry.reactivation_error, str) or not entry.reactivation_error.strip()
        ):
            raise ValueError("Invalid reactivation error")
        return entry


@dataclass(frozen=True, kw_only=True)
class ImportedItem:
    """A current enrollment, original creation identity, and historical attempts."""

    item: QueueItem
    command: EnqueueCommand
    deliveries: tuple[Delivery, ...] = ()


@dataclass(frozen=True, kw_only=True)
class QueueImport:
    """Fully interpreted legacy records; opaque history remains recoverable metadata."""

    items: tuple[ImportedItem, ...] = ()
    opaque_history: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, kw_only=True)
class LegacyQueue:
    """Read-only retired queue. Only this module knows its representation and merge rules."""

    entries: tuple[LegacyEntry, ...] = ()
    history: tuple[Any, ...] = ()

    @classmethod
    def read(cls, path: Path) -> "LegacyQueue":
        """Read a version 1 empty or version 2 JSON queue, ignoring removed pause state."""
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise TypeError("Invalid queue file")
        if raw.get("version") == 1:
            if raw.get("entries"):
                raise ValueError(
                    "Re-enroll legacy entries before migrating to the independent monitor"
                )
        elif raw.get("version") != 2:
            raise ValueError("Unsupported queue file format")
        if not isinstance(raw.get("entries"), list) or not isinstance(raw.get("history"), list):
            raise TypeError("Invalid queue entries or history")
        entries = tuple(LegacyEntry.parse(entry) for entry in raw["entries"])
        if len({entry.thread_id for entry in entries}) != len(entries):
            raise ValueError("Duplicate chat enrollment in queue")
        return cls(entries=entries, history=tuple(raw["history"]))

    def merge(self, current: "LegacyQueue", now: float) -> "QueueImport":
        """Prefer terminal history, then uncertain sends, with current entries winning ties.

        Each chat retains a single enrollment. Superseded enrollments become
        cancelled history. No merged copy of the retired format is written.
        """
        history: list[Any] = []
        for record in self.history + current.history:
            if record not in history:
                history.append(record)
        finished = {record.get("id") for record in history if isinstance(record, dict)}
        entries = {entry.thread_id: entry for entry in current.entries if entry.id not in finished}
        retired: list[LegacyEntry] = []
        priority = {"waiting": 0, "sending": 1, "needs_attention": 2}
        for entry in self.entries:
            if entry.id in finished:
                continue
            existing = entries.get(entry.thread_id)
            if existing is None:
                entries[entry.thread_id] = entry
                continue
            if priority[entry.state] > priority[existing.state]:
                winner, loser = entry, existing
            else:
                winner, loser = existing, entry
            entries[entry.thread_id] = winner
            if loser.id != winner.id:
                retired.append(loser)

        imported: list[ImportedItem] = []
        opaque: list[tuple[str, str]] = []
        for raw in history:
            try:
                if not isinstance(raw, dict):
                    raise TypeError("Partial history")
                fields = {
                    key: value
                    for key, value in raw.items()
                    if key not in {"outcome", "finished_at"}
                }
                entry = LegacyEntry.parse(fields)
                finished_at = raw.get("finished_at", now)
                if type(finished_at) not in (int, float) or not math.isfinite(finished_at):
                    raise ValueError("Invalid completion timestamp")
            except (ValueError, TypeError, KeyError):
                payload = json.dumps(raw, sort_keys=True, allow_nan=False)
                digest = hashlib.sha256(payload.encode()).hexdigest()
                opaque.append(("legacy_history:" + digest, payload))
                continue
            outcome = raw.get("outcome")
            if outcome == "resumed":
                state: ItemState = "sent"
            elif outcome in {"cancelled", "superseded_by_migration"}:
                state = "cancelled"
            else:
                state = "changed"
            imported.append(_translate(entry, state, now, finished_at))
        imported.extend(_translate(entry, "cancelled", now, now) for entry in retired)
        for entry in entries.values():
            state = "changed" if entry.state == "needs_attention" else "queued"
            imported.append(
                _translate(
                    entry,
                    state,
                    now,
                    None if state == "queued" else now,
                    current_enrollment=True,
                )
            )
        return QueueImport(items=tuple(imported), opaque_history=tuple(opaque))

    def normalize(self, now: float) -> QueueImport:
        """Translate a single queue using the same precedence rules as installation merge."""
        return LegacyQueue().merge(self, now)


def _translate(
    entry: LegacyEntry,
    state: ItemState,
    now: float,
    finished_at: float | None,
    *,
    current_enrollment: bool = False,
) -> ImportedItem:
    # Legacy files did not record enrollment time. Attempt time belongs to the
    # delivery, while import order ensures a retained enrollment follows history.
    terminal_time = finished_at if finished_at is not None else now
    queued_at = now if current_enrollment else min(terminal_time, now - 1)
    item = QueueItem(
        id=entry.id,
        chat_id=entry.thread_id,
        title=entry.title,
        account_id=entry.account_id,
        limit_id=entry.limit_id,
        anchor_message_id=entry.anchor_message_id,
        model=entry.model,
        instruction=entry.prompt,
        reset_windows=dict(entry.reset_windows),
        enrolled_while_busy=entry.enrolled_while_busy,
        queued_at=queued_at,
        updated_at=now,
        state=state,
        reason=entry.note,
        error=entry.reactivation_error,
        finished_at=finished_at,
    )
    command = EnqueueCommand(
        id=item.id,
        chat_id=item.chat_id,
        instruction=item.instruction,
        limit_id=item.limit_id,
    )
    deliveries: tuple[Delivery, ...] = ()
    if entry.attempted_at is not None:
        attempt = Delivery(
            id=str(uuid5(UUID(entry.id), "legacy-delivery")),
            queue_item_id=item.id,
            instruction=item.instruction,
            started_at=entry.attempted_at,
            lease_expires_at=entry.attempted_at + 120,
            sending_at=entry.attempted_at,
            finished_at=finished_at if finished_at is not None else now,
            outcome="sent" if state == "sent" else "expired",
            reason="Imported previous delivery attempt",
        )
        deliveries = (attempt,)
    return ImportedItem(item=item, command=command, deliveries=deliveries)

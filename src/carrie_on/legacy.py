"""Read saved JSON queues and convert their records to queue entries."""

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from carrie_on.ports import QueueStorage
from carrie_on.queue import PENDING_STATES, EntryState, QueueEntry, QueueError


@dataclass(frozen=True, kw_only=True)
class LegacyEntry:
    """Represent an entry from version 2 of the JSON queue format."""

    id: str
    thread_id: str
    title: str
    account_id: str
    limit_id: str
    anchor_message_id: str | None
    model: str
    prompt: str
    reset_windows: dict[str, int]
    enrolled_while_busy: bool
    anchor_turn_id: str | None = None
    accepted_turn_id: str | None = None
    state: Literal["waiting", "sending", "needs_attention"] = "waiting"
    note: str = "Waiting for the recorded usage reset"
    attempted_at: float | None = None
    reactivation_error: str | None = None
    processing_until: float | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "LegacyEntry":
        """Check the saved entry's fields without applying new-request text restrictions."""
        entry = cls(**raw)
        if not isinstance(entry.id, str):
            raise TypeError("Invalid queue entry ID")
        UUID(entry.id)
        UUID(entry.thread_id)
        for value in (
            entry.thread_id,
            entry.title,
            entry.account_id,
            entry.limit_id,
            entry.model,
            entry.prompt,
            entry.note,
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Invalid queue entry text field")
        if not (entry.anchor_message_id or entry.anchor_turn_id):
            raise ValueError("Missing conversation anchor")
        for identity in (entry.anchor_message_id, entry.anchor_turn_id, entry.accepted_turn_id):
            if identity is not None and (not isinstance(identity, str) or not identity.strip()):
                raise ValueError("Invalid conversation identity")
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

    def to_entry(
        self,
        state: EntryState,
        now: float,
        finished_at: float | None,
        *,
        current_enrollment: bool = False,
    ) -> QueueEntry:
        """Convert a saved JSON record to a QueueEntry with the supplied state and times."""
        # JSON queues did not save enqueue times. Place imported history before pending work.
        terminal_time = finished_at if finished_at is not None else now
        queued_at = now if current_enrollment else min(terminal_time, now - 1)
        return QueueEntry(
            id=UUID(self.id),
            chat_id=UUID(self.thread_id),
            title=self.title,
            account_id=self.account_id,
            limit_id=self.limit_id,
            anchor_message_id=self.anchor_message_id,
            anchor_turn_id=self.anchor_turn_id,
            model=self.model,
            instructions=self.prompt,
            reset_windows=dict(self.reset_windows),
            enrolled_while_busy=self.enrolled_while_busy,
            queued_at=datetime.fromtimestamp(queued_at, timezone.utc),
            updated_at=datetime.fromtimestamp(now, timezone.utc),
            state=state,
            error=(self.reactivation_error or self.note) if state == "failed" else "",
            finished_at=datetime.fromtimestamp(finished_at, timezone.utc)
            if finished_at is not None
            else None,
        )


@dataclass(frozen=True, kw_only=True)
class LegacyQueue:
    """Read and merge saved JSON queues without changing the source files."""

    entries: tuple[LegacyEntry, ...] = ()
    history: tuple[Any, ...] = ()

    @classmethod
    def read(cls, path: Path) -> "LegacyQueue":
        """Read a version 2 JSON queue or an empty version 1 queue. Ignore the pause setting."""
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

    def merge(self, current: "LegacyQueue", now: float) -> tuple[QueueEntry, ...]:
        """Merge saved queues into QueueEntry records without writing a merged JSON file.

        History takes precedence over pending entries. For each chat, needs_attention
        takes precedence over sending, then waiting. Entries from current win ties.
        Keep the other entries as cancelled history.
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

        imported: list[QueueEntry] = []
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
                # The source snapshot keeps history records that cannot be imported.
                continue
            outcome = raw.get("outcome")
            if outcome == "resumed":
                state: EntryState = "sent"
            elif outcome in {"cancelled", "superseded_by_migration"}:
                state = "cancelled"
            else:
                state = "stale"
            imported.append(entry.to_entry(state, now, finished_at))
        imported.extend(entry.to_entry("cancelled", now, now) for entry in retired)
        for entry in entries.values():
            state = (
                "sent"
                if entry.accepted_turn_id
                else ("failed" if entry.state == "needs_attention" else "queued")
            )
            imported.append(
                entry.to_entry(
                    state,
                    now,
                    None if state in PENDING_STATES else now,
                    current_enrollment=True,
                )
            )
        return tuple(imported)

    def normalize(self, now: float) -> tuple[QueueEntry, ...]:
        """Convert this queue's records using the same rules as merging installations."""
        return LegacyQueue().merge(self, now)


class LegacyImporter:
    """Import saved JSON queue entries before the server accepts requests.

    Insert entries individually and skip UUIDs already in storage. An interrupted
    import can resume without overwriting edits or reactivating finished entries.
    Keep the source JSON for history and attempt details that cannot be imported.
    """

    def __init__(self, storage: QueueStorage):
        self.storage = storage

    def import_file(self, path: Path) -> bool:
        if not path.exists():
            return False
        return self.import_records(LegacyQueue.read(path).normalize(datetime.now().timestamp()))

    def import_records(self, records: Sequence[QueueEntry]) -> bool:
        source_ids = {entry.id for entry in records}
        current: dict[UUID, datetime] = {}
        cursor = None
        while True:
            entries, cursor = self.storage.list_entries(states=None, limit=100, cursor=cursor)
            for entry in entries:
                if entry.id not in source_ids:
                    current.setdefault(entry.chat_id, entry.queued_at)
            if cursor is None:
                break
        imported = False
        for entry in records:
            if self.storage.get_entry(entry.id) is not None:
                continue
            if entry.chat_id in current:
                entry = replace(
                    entry,
                    queued_at=min(entry.queued_at, current[entry.chat_id] - timedelta(seconds=1)),
                )
                if entry.state in PENDING_STATES:
                    entry = replace(
                        entry, state="cancelled", error="", finished_at=entry.updated_at
                    )
            try:
                self.storage.create_entry(entry)
            except QueueError as error:
                if error.code != "entry_exists":
                    raise
            else:
                imported = True
        return imported

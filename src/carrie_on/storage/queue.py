"""Read and write queue entries and delivery attempts in SQLite."""

import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from carrie_on.pagination import decode_cursor, encode_cursor
from carrie_on.queue import (
    DeliveryAttempt,
    EntryState,
    QueueEntry,
    QueueError,
    QueueSummary,
)
from carrie_on.storage.database import Database


class SqliteQueueStorage:
    """Store queue entries and claim work through delivery attempts.

    Each operation opens its own transaction. The service uses its clock and lease
    duration to set attempt expiry times. It makes no Codex calls.
    """

    _select = """SELECT q.*, (q.state IN ('queued','failed') AND EXISTS (
        SELECT 1 FROM delivery_attempts d WHERE d.entry_id=q.id AND d.expires_at>?
    )) AS dispatching FROM queue_entries q"""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        lease_duration: timedelta = timedelta(seconds=120),
    ):
        if lease_duration <= timedelta(0):
            raise ValueError("Delivery lease duration must be positive")
        self._database = Database(path)
        self._clock = clock
        self._lease_duration = lease_duration

    @staticmethod
    def _entry(row: sqlite3.Row) -> QueueEntry:
        return QueueEntry(
            id=UUID(row["id"]),
            chat_id=UUID(row["chat_id"]),
            title=row["title"],
            account_id=row["account_id"],
            limit_id=row["limit_id"],
            anchor_message_id=row["anchor_message_id"],
            anchor_turn_id=row["anchor_turn_id"],
            model=row["model"],
            instructions=row["instructions"],
            reset_windows=json.loads(row["reset_windows"]),
            enrolled_while_busy=bool(row["enrolled_while_busy"]),
            queued_at=datetime.fromtimestamp(row["queued_at"], timezone.utc),
            updated_at=datetime.fromtimestamp(row["updated_at"], timezone.utc),
            state=row["state"],
            error=row["error"],
            revision=row["revision"],
            finished_at=datetime.fromtimestamp(row["finished_at"], timezone.utc)
            if row["finished_at"] is not None
            else None,
            dispatching=bool(row["dispatching"]),
        )

    def _get(
        self, connection: sqlite3.Connection, entry_id: UUID, now: datetime
    ) -> QueueEntry | None:
        row = connection.execute(
            self._select + " WHERE q.id=?", (now.timestamp(), str(entry_id))
        ).fetchone()
        return self._entry(row) if row is not None else None

    def get_entry(self, entry_id: UUID) -> QueueEntry | None:
        with self._database.connection() as connection:
            return self._get(connection, entry_id, self._clock())

    def list_entries(
        self,
        *,
        states: tuple[EntryState, ...] | None = ("queued", "failed"),
        chat_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[QueueEntry], str | None]:
        if not 1 <= limit <= 100:
            raise ValueError("Page limit must be between 1 and 100")
        scope = [
            "queue",
            list(states) if states is not None else None,
            str(chat_id) if chat_id else None,
        ]
        conditions: list[str] = []
        parameters: list[Any] = [self._clock().timestamp()]
        if states is not None:
            conditions.append("q.state IN (" + ",".join("?" for _ in states) + ")")
            parameters.extend(states)
        if chat_id is not None:
            conditions.append("q.chat_id=?")
            parameters.append(str(chat_id))
        if cursor is not None:
            position = decode_cursor(cursor, scope)
            if (
                not isinstance(position, list)
                or len(position) != 2
                or not (
                    type(position[0]) is int
                    and -(2**63) <= position[0] < 2**63
                    or type(position[0]) is float
                    and math.isfinite(position[0])
                )
                or not isinstance(position[1], str)
            ):
                raise QueueError("invalid_cursor", "Invalid queue position")
            try:
                UUID(position[1])
            except ValueError as error:
                raise QueueError("invalid_cursor", "Invalid queue identity") from error
            conditions.append("(q.queued_at,q.id)>(?,?)")
            parameters.extend(position)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._database.connection() as connection:
            rows = connection.execute(
                self._select + where + " ORDER BY q.queued_at,q.id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        entries = [self._entry(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = entries[-1]
            next_cursor = encode_cursor(scope, [last.queued_at.timestamp(), str(last.id)])
        return entries, next_cursor

    def create_entry(self, entry: QueueEntry) -> QueueEntry:
        with self._database.connection(write=True) as connection:
            try:
                connection.execute(
                    """INSERT INTO queue_entries (
                id,chat_id,title,account_id,limit_id,anchor_message_id,model,instructions,
                reset_windows,enrolled_while_busy,queued_at,updated_at,
                state,error,revision,finished_at,anchor_turn_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(entry.id),
                        str(entry.chat_id),
                        entry.title,
                        entry.account_id,
                        entry.limit_id,
                        entry.anchor_message_id,
                        entry.model,
                        entry.instructions,
                        json.dumps(entry.reset_windows),
                        entry.enrolled_while_busy,
                        entry.queued_at.timestamp(),
                        entry.updated_at.timestamp(),
                        entry.state,
                        entry.error,
                        entry.revision,
                        entry.finished_at.timestamp() if entry.finished_at is not None else None,
                        entry.anchor_turn_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                if connection.execute(
                    "SELECT 1 FROM queue_entries WHERE id=?", (str(entry.id),)
                ).fetchone():
                    raise QueueError("entry_exists", "Queue entry already exists") from error
                if connection.execute(
                    "SELECT 1 FROM queue_entries WHERE chat_id=? AND state IN ('queued','failed')",
                    (str(entry.chat_id),),
                ).fetchone():
                    raise QueueError("chat_already_queued", "Chat is already queued") from error
                raise
            return replace(entry, dispatching=False)

    def update_instructions(
        self, entry_id: UUID, instructions: str, *, revision: int
    ) -> QueueEntry:
        now = self._clock()
        with self._database.connection(write=True) as connection:
            entry = self._get(connection, entry_id, now)
            if entry is None:
                raise QueueError("not_found", "Queue entry not found")
            if entry.revision != revision:
                raise QueueError(
                    "stale_revision", "The entry changed; read it before editing again"
                )
            updated = entry.update_instructions(instructions, now)
            connection.execute(
                "UPDATE queue_entries SET instructions=?,updated_at=?,revision=revision+1 WHERE id=?",
                (updated.instructions, now.timestamp(), str(entry_id)),
            )
            return replace(updated, revision=entry.revision + 1)

    def update_entry_state(
        self, entry_id: UUID, state: EntryState, *, error: str = ""
    ) -> QueueEntry:
        now = self._clock()
        with self._database.connection(write=True) as connection:
            entry = self._get(connection, entry_id, now)
            if entry is None:
                raise QueueError("not_found", "Queue entry not found")
            updated = entry.update_state(state, now, error=error)
            if updated is entry:
                return entry
            connection.execute(
                """UPDATE queue_entries SET state=?,error=?,updated_at=?,
                finished_at=?,revision=revision+1 WHERE id=?""",
                (
                    updated.state,
                    updated.error,
                    now.timestamp(),
                    updated.finished_at.timestamp() if updated.finished_at is not None else None,
                    str(entry_id),
                ),
            )
            return replace(updated, revision=entry.revision + 1)

    def create_delivery_attempt(self) -> DeliveryAttempt | None:
        with self._database.connection(write=True) as connection:
            now = self._clock()
            row = connection.execute(
                self._select
                + """ WHERE q.state IN ('queued','failed')
                AND NOT EXISTS (SELECT 1 FROM delivery_attempts d
                    WHERE d.entry_id=q.id AND d.expires_at>?)
                ORDER BY q.queued_at,q.id LIMIT 1""",
                (now.timestamp(), now.timestamp()),
            ).fetchone()
            if row is None:
                return None
            entry = replace(self._entry(row), dispatching=True)
            attempt = DeliveryAttempt(
                id=uuid4(), entry=entry, created_at=now, expires_at=now + self._lease_duration
            )
            connection.execute(
                "INSERT INTO delivery_attempts VALUES (?,?,?,?)",
                (
                    str(attempt.id),
                    str(entry.id),
                    now.timestamp(),
                    attempt.expires_at.timestamp(),
                ),
            )
            return attempt

    def get_queue_summary(self) -> QueueSummary:
        with self._database.connection() as connection:
            counts: dict[EntryState, int] = {
                "queued": 0,
                "failed": 0,
                "stale": 0,
                "cancelled": 0,
                "sent": 0,
            }
            counts.update(
                connection.execute(
                    "SELECT state,count(*) FROM queue_entries GROUP BY state"
                ).fetchall()
            )
            dispatching = connection.execute(
                """SELECT count(*) FROM queue_entries q
                WHERE q.state IN ('queued','failed') AND EXISTS (
                    SELECT 1 FROM delivery_attempts d WHERE d.entry_id=q.id AND d.expires_at>?
                )""",
                (self._clock().timestamp(),),
            ).fetchone()[0]
        return QueueSummary(counts=counts, dispatching=dispatching)

"""SQLite transactions for queue membership, ordering, and delivery leases."""

import base64
import json
import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from carrie_on.legacy import LegacyQueue, QueueImport
from carrie_on.queue import (
    ActiveDelivery,
    CompletionOutcome,
    Delivery,
    EnqueueCommand,
    QueueEntry,
    QueueError,
    QueueItem,
)

SCHEMA_VERSION = 2

QUEUE_TABLE = """CREATE TABLE queue_items (
    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, title TEXT NOT NULL,
    account_id TEXT NOT NULL, limit_id TEXT NOT NULL, anchor_message_id TEXT NOT NULL,
    model TEXT NOT NULL, instruction TEXT NOT NULL,
    reset_windows TEXT NOT NULL, enrolled_while_busy INTEGER NOT NULL CHECK(enrolled_while_busy IN (0,1)),
    queued_at REAL NOT NULL, updated_at REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','sent','changed','cancelled','failed')),
    reason TEXT NOT NULL, deferrals INTEGER NOT NULL DEFAULT 0 CHECK(deferrals >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0), error TEXT, finished_at REAL,
    create_request TEXT NOT NULL
)"""
QUEUE_INDEXES = (
    "CREATE UNIQUE INDEX queued_chat ON queue_items(chat_id) WHERE state = 'queued'",
    "CREATE INDEX queue_order ON queue_items(state, deferrals, queued_at, id)",
)
SCHEMA = (
    QUEUE_TABLE,
    *QUEUE_INDEXES,
    """CREATE TABLE deliveries (
        id TEXT PRIMARY KEY, queue_item_id TEXT NOT NULL REFERENCES queue_items(id),
        instruction TEXT NOT NULL, started_at REAL NOT NULL, lease_expires_at REAL NOT NULL,
        sending_at REAL, finished_at REAL,
        outcome TEXT CHECK(outcome IN ('sent','changed','deferred','failed','expired')),
        reason TEXT, receipt TEXT,
        CHECK(lease_expires_at > started_at),
        CHECK((finished_at IS NULL) = (outcome IS NULL))
    )""",
    "CREATE UNIQUE INDEX unfinished_delivery ON deliveries(queue_item_id) WHERE finished_at IS NULL",
    "CREATE INDEX delivery_history ON deliveries(queue_item_id, started_at, id)",
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
ITEM_SELECT = """SELECT q.*, d.id AS delivery_id, d.lease_expires_at, d.sending_at
    FROM queue_items q LEFT JOIN deliveries d ON d.queue_item_id=q.id AND d.finished_at IS NULL"""
OPEN_CONDITION = """(q.state='queued' OR (q.state IN ('changed','failed') AND NOT EXISTS (
    SELECT 1 FROM queue_items newer WHERE newer.chat_id=q.chat_id
    AND (newer.state='queued' OR (newer.queued_at,newer.id)>(q.queued_at,q.id)))))"""


class QueueSummary(TypedDict):
    count: int
    needs_attention: int
    last_check: float | None


def cursor_encode(scope: list[Any], position: Any) -> str:
    """Encode a position bound to the exact list query that produced it."""
    payload = json.dumps([scope, position], separators=(",", ":"), allow_nan=False).encode()
    return base64.urlsafe_b64encode(payload).decode()


def cursor_decode(cursor: str, scope: list[Any]) -> Any:
    """Decode query identity; each caller validates its position before SQL binding."""
    try:
        saved_scope, position = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if saved_scope != scope:
            raise ValueError("Query changed")
        return position
    except (ValueError, TypeError, UnicodeError) as error:
        raise QueueError("invalid_cursor", "Cursor does not belong to this query") from error


def _timestamp(value: Any) -> bool:
    return (
        type(value) is int
        and -(2**63) <= value < 2**63
        or type(value) is float
        and math.isfinite(value)
    )


def _creation_payload(command: EnqueueCommand) -> dict[str, Any]:
    return {
        "chat_id": command.chat_id,
        "instruction": command.instruction,
        "limit_id": command.limit_id,
    }


class QueueStore:
    """Own SQLite membership, ordering, creation identity, and delivery ownership.

    Methods use independent connections and short transactions. Call initialize
    before serving requests. Input commands have already crossed validation at
    the application boundary; persisted instruction text is stored verbatim.

    A worker claims an item, evaluates fresh observations, optionally calls
    begin_send, then finishes its attempt. Every ownership check uses the same
    Unix-seconds clock; a False result means the lease no longer belongs to the
    caller and no state was changed.

    Worker integration example (send returns an accepted turn ID)::

        claimed = store.claim()
        if claimed is not None:
            item, delivery = claimed
            if fresh_observations_allow_send(item) and store.begin_send(delivery):
                receipt = send(item.chat_id, delivery.instruction, item.id)
                store.finish(delivery, "sent", "Accepted", receipt=receipt)

    When observations defer or finish work without a send, call finish directly
    with that decision. Do not send after begin_send returns False.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a private connection; write transactions commit or roll back as a unit."""
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, legacy_path: Path | None = None) -> None:
        """Create or upgrade atomically, importing a retired JSON queue at most once.

        The caller must stop legacy writers first. The original file is retained
        unchanged for recovery; malformed input rolls back schema and import.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            # Rebuilding the parent table preserves delivery references. Foreign
            # keys are checked before commit and enabled on every later connection.
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise ValueError("Database was created by a newer Carrie On version")
                if version == 0:
                    for statement in SCHEMA:
                        connection.execute(statement)
                elif version == 1:
                    connection.execute(QUEUE_TABLE.replace("queue_items", "queue_items_new", 1))
                    connection.execute("INSERT INTO queue_items_new SELECT * FROM queue_items")
                    connection.execute("DROP TABLE queue_items")
                    connection.execute("ALTER TABLE queue_items_new RENAME TO queue_items")
                    for statement in QUEUE_INDEXES:
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                imported = connection.execute(
                    "SELECT 1 FROM metadata WHERE key='json_import'"
                ).fetchone()
                if not imported:
                    if legacy_path is not None:
                        records = LegacyQueue.read(Path(legacy_path)).normalize(time.time())
                        self._import(connection, records)
                    connection.execute(
                        "INSERT INTO metadata VALUES ('json_import', ?)",
                        (json.dumps({"path": str(legacy_path), "at": time.time()}),),
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Database contains invalid delivery references")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def import_legacy_snapshot(self, path: Path, marker: str) -> None:
        """Import an immutable JSON recovery copy once without replacing current work."""
        self.import_legacy(LegacyQueue.read(path).normalize(time.time()), marker)

    def import_legacy(self, records: QueueImport, marker: str) -> None:
        """Atomically import normalized records and their durable retry marker."""
        with self.connection(write=True) as connection:
            if connection.execute("SELECT 1 FROM metadata WHERE key=?", (marker,)).fetchone():
                return
            self._import(connection, records)
            connection.execute(
                "INSERT INTO metadata VALUES (?,?)", (marker, json.dumps(time.time()))
            )

    def _import(self, connection: sqlite3.Connection, records: QueueImport) -> None:
        current_chats = dict(
            connection.execute(
                "SELECT chat_id,min(queued_at) FROM queue_items GROUP BY chat_id"
            ).fetchall()
        )
        for key, payload in records.opaque_history:
            connection.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, payload))
        for record in records.items:
            item = record.item
            if connection.execute("SELECT 1 FROM queue_items WHERE id=?", (item.id,)).fetchone():
                continue
            if item.chat_id in current_chats:
                item = replace(item, queued_at=min(item.queued_at, current_chats[item.chat_id] - 1))
                if item.state == "queued":
                    item = replace(
                        item,
                        state="cancelled",
                        finished_at=item.updated_at,
                        reason="Preserved in history because this chat already has SQLite queue history",
                    )
            self._insert(connection, item, record.command)
            for delivery in record.deliveries:
                self._insert_delivery(connection, delivery)

    @staticmethod
    def _entry(row: sqlite3.Row | None) -> QueueEntry:
        if row is None:
            raise QueueError("not_found", "Queue item not found")
        item = QueueItem(
            id=row["id"],
            chat_id=row["chat_id"],
            title=row["title"],
            account_id=row["account_id"],
            limit_id=row["limit_id"],
            anchor_message_id=row["anchor_message_id"],
            model=row["model"],
            instruction=row["instruction"],
            reset_windows=json.loads(row["reset_windows"]),
            enrolled_while_busy=bool(row["enrolled_while_busy"]),
            queued_at=row["queued_at"],
            updated_at=row["updated_at"],
            state=row["state"],
            reason=row["reason"],
            deferrals=row["deferrals"],
            revision=row["revision"],
            error=row["error"],
            finished_at=row["finished_at"],
        )
        active = None
        if row["delivery_id"] is not None:
            active = ActiveDelivery(
                id=row["delivery_id"],
                lease_expires_at=row["lease_expires_at"],
                sending_at=row["sending_at"],
            )
        return QueueEntry(item=item, active_delivery=active)

    def _get(self, connection: sqlite3.Connection, item_id: str) -> QueueEntry:
        return self._entry(connection.execute(ITEM_SELECT + " WHERE q.id=?", (item_id,)).fetchone())

    def get(self, item_id: str) -> QueueEntry:
        """Read an enrollment and its unfinished attempt, or raise not_found."""
        with self.connection() as connection:
            return self._get(connection, item_id)

    def replay(self, command: EnqueueCommand) -> QueueEntry | None:
        """Recover an original creation across edits/completion; conflicting input raises id_reused."""
        with self.connection() as connection:
            return self._replay(connection, command)

    def _replay(self, connection: sqlite3.Connection, command: EnqueueCommand) -> QueueEntry | None:
        row = connection.execute(
            "SELECT create_request FROM queue_items WHERE id=?", (command.id,)
        ).fetchone()
        if row is None:
            return None
        if json.loads(row[0]) != _creation_payload(command):
            raise QueueError(
                "id_reused", "This UUID was already used with different creation input"
            )
        return self._get(connection, command.id)

    @staticmethod
    def _insert(connection: sqlite3.Connection, item: QueueItem, command: EnqueueCommand) -> None:
        connection.execute(
            """INSERT INTO queue_items (
            id,chat_id,title,account_id,limit_id,anchor_message_id,model,instruction,
            reset_windows,enrolled_while_busy,queued_at,updated_at,state,reason,deferrals,
            revision,error,finished_at,create_request) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.id,
                item.chat_id,
                item.title,
                item.account_id,
                item.limit_id,
                item.anchor_message_id,
                item.model,
                item.instruction,
                json.dumps(item.reset_windows),
                item.enrolled_while_busy,
                item.queued_at,
                item.updated_at,
                item.state,
                item.reason,
                item.deferrals,
                item.revision,
                item.error,
                item.finished_at,
                json.dumps(_creation_payload(command), sort_keys=True),
            ),
        )

    def enqueue(self, item: QueueItem, command: EnqueueCommand) -> tuple[QueueEntry, bool]:
        """Insert or replay an enrollment; new chats join the current minimum deferral count."""
        with self.connection(write=True) as connection:
            existing = self._replay(connection, command)
            if existing is not None:
                return existing, False
            if connection.execute(
                "SELECT 1 FROM queue_items WHERE chat_id=? AND state='queued'",
                (item.chat_id,),
            ).fetchone():
                raise QueueError("chat_already_queued", "Chat is already queued")
            self._insert(connection, item, command)
            connection.execute(
                """UPDATE queue_items SET deferrals=COALESCE(
                (SELECT MIN(deferrals) FROM queue_items WHERE state='queued' AND id != ?),0) WHERE id=?""",
                (item.id, item.id),
            )
            return self._get(connection, item.id), True

    def page(
        self,
        *,
        state: str = "open",
        chat_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[QueueEntry], str | None]:
        """List queue entries ordered by deferrals, enrollment time and UUID; cursors bind filters."""
        scope = ["queue", state, chat_id]
        conditions: list[str] = []
        parameters: list[Any] = []
        if state == "open":
            conditions.append(OPEN_CONDITION)
        elif state != "all":
            if state not in {"queued", "sent", "changed", "failed", "cancelled"}:
                raise ValueError("Unknown queue state")
            conditions.append("q.state=?")
            parameters.append(state)
        if chat_id:
            conditions.append("q.chat_id=?")
            parameters.append(chat_id)
        if cursor:
            position = cursor_decode(cursor, scope)
            if (
                not isinstance(position, list)
                or len(position) != 3
                or type(position[0]) is not int
                or not 0 <= position[0] < 2**63
                or not _timestamp(position[1])
                or not isinstance(position[2], str)
            ):
                raise QueueError("invalid_cursor", "Invalid queue cursor")
            conditions.append("(q.deferrals,q.queued_at,q.id)>(?,?,?)")
            parameters.extend(position)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connection() as connection:
            rows = connection.execute(
                ITEM_SELECT + where + " ORDER BY q.deferrals,q.queued_at,q.id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        entries = [self._entry(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = entries[-1].item
            next_cursor = cursor_encode(scope, [last.deferrals, last.queued_at, last.id])
        return entries, next_cursor

    @staticmethod
    def _require_no_active_delivery(entry: QueueEntry, now: float) -> None:
        if entry.active_delivery is not None and entry.active_delivery.lease_expires_at > now:
            raise QueueError("delivery_active", "A delivery is active; wait for it to finish")

    def edit(
        self,
        item_id: str,
        instruction: str,
        expected_revision: int,
        now: float | None = None,
    ) -> QueueEntry:
        """Replace exact instruction text for an idle queued item at the expected revision."""
        now = time.time() if now is None else now
        with self.connection(write=True) as connection:
            entry = self._get(connection, item_id)
            self._require_no_active_delivery(entry, now)
            if entry.item.state != "queued":
                raise QueueError("item_finished", "Only queued instructions can be edited")
            if entry.item.revision != expected_revision:
                raise QueueError("stale_revision", "The item changed; read it before editing again")
            connection.execute(
                "UPDATE queue_items SET instruction=?,updated_at=?,revision=revision+1 WHERE id=?",
                (instruction, now, item_id),
            )
            return self._get(connection, item_id)

    def cancel(self, item_id: str, now: float | None = None) -> QueueEntry:
        """Cancel idle work idempotently; accepted sends and active leases cannot be cancelled."""
        now = time.time() if now is None else now
        with self.connection(write=True) as connection:
            entry = self._get(connection, item_id)
            self._require_no_active_delivery(entry, now)
            if entry.item.state == "cancelled":
                return entry
            if entry.item.state == "sent":
                raise QueueError("item_finished", "The message was already sent")
            self._expire(connection, item_id, now)
            connection.execute(
                """UPDATE queue_items SET state='cancelled',finished_at=?,updated_at=?,
                revision=revision+1 WHERE id=?""",
                (now, now, item_id),
            )
            return self._get(connection, item_id)

    @staticmethod
    def _expire(connection: sqlite3.Connection, item_id: str, now: float) -> None:
        connection.execute(
            """UPDATE deliveries SET finished_at=?,outcome='expired',reason='Lease expired'
            WHERE queue_item_id=? AND finished_at IS NULL AND lease_expires_at<=?""",
            (now, item_id, now),
        )

    @staticmethod
    def _insert_delivery(connection: sqlite3.Connection, delivery: Delivery) -> None:
        connection.execute(
            """INSERT INTO deliveries (
            id,queue_item_id,instruction,started_at,lease_expires_at,sending_at,finished_at,outcome,reason,receipt)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                delivery.id,
                delivery.queue_item_id,
                delivery.instruction,
                delivery.started_at,
                delivery.lease_expires_at,
                delivery.sending_at,
                delivery.finished_at,
                delivery.outcome,
                delivery.reason,
                delivery.receipt,
            ),
        )

    def claim(
        self, *, now: float | None = None, lease_seconds: float = 120
    ) -> tuple[QueueItem, Delivery] | None:
        """Claim the next queued item without a live lease, atomically across workers.

        now is Unix seconds (defaults to wall clock); lease_seconds is positive.
        The returned delivery owns this item until its expiry. Expired attempts
        remain in history, and an empty eligible queue returns None. See the
        QueueStore class example for the claim → begin_send → finish lifecycle.
        """
        now = time.time() if now is None else now
        with self.connection(write=True) as connection:
            row = connection.execute(
                ITEM_SELECT
                + """ WHERE q.state='queued'
                AND (d.id IS NULL OR d.lease_expires_at<=?)
                ORDER BY q.deferrals,q.queued_at,q.id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            item = self._entry(row).item
            self._expire(connection, item.id, now)
            delivery = Delivery(
                id=str(uuid4()),
                queue_item_id=item.id,
                instruction=item.instruction,
                started_at=now,
                lease_expires_at=now + lease_seconds,
            )
            self._insert_delivery(connection, delivery)
            connection.execute(
                "UPDATE queue_items SET revision=revision+1,updated_at=? WHERE id=?",
                (now, item.id),
            )
            return self._get(connection, item.id).item, delivery

    @staticmethod
    def _owns(connection: sqlite3.Connection, delivery: Delivery, now: float) -> bool:
        return (
            connection.execute(
                """SELECT 1 FROM deliveries d JOIN queue_items q ON q.id=d.queue_item_id
            WHERE d.id=? AND d.finished_at IS NULL AND d.lease_expires_at>? AND q.state='queued'""",
                (delivery.id, now),
            ).fetchone()
            is not None
        )

    def begin_send(self, delivery: Delivery, now: float | None = None) -> bool:
        """Record send intent while the lease is owned; return False without changes if lost.

        now is Unix seconds. This fence does not make the external send atomic;
        callers must use the queue item's stable UUID and reconcile fresh chat
        state after a crash. See QueueStore for the complete worker lifecycle.
        """
        now = time.time() if now is None else now
        with self.connection(write=True) as connection:
            if not self._owns(connection, delivery, now):
                return False
            connection.execute("UPDATE deliveries SET sending_at=? WHERE id=?", (now, delivery.id))
            connection.execute(
                """UPDATE queue_items SET revision=revision+1,updated_at=?,error=NULL,
                reason='Sending instruction' WHERE id=?""",
                (now, delivery.queue_item_id),
            )
            return True

    def finish(
        self,
        delivery: Delivery,
        outcome: CompletionOutcome,
        reason: str,
        *,
        error: str | None = None,
        receipt: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Complete an owned attempt and its item atomically; lost leases change nothing.

        Outcomes are sent, changed, failed, or deferred. Deferral leaves the item
        queued and increments its ordering counter. Expiry is owned by claim,
        never a caller completion. now is Unix seconds; receipt is an accepted
        Codex turn ID. Return True for a committed completion, False after lease
        loss or a previous completion. See QueueStore for the worker lifecycle.
        """
        now = time.time() if now is None else now
        state = "queued" if outcome == "deferred" else outcome
        with self.connection(write=True) as connection:
            if not self._owns(connection, delivery, now):
                return False
            connection.execute(
                """UPDATE deliveries SET finished_at=?,outcome=?,reason=?,receipt=? WHERE id=?""",
                (now, outcome, reason, receipt, delivery.id),
            )
            connection.execute(
                """UPDATE queue_items SET state=?,reason=?,error=?,updated_at=?,
                finished_at=?,deferrals=deferrals+?,revision=revision+1 WHERE id=?""",
                (
                    state,
                    reason,
                    error,
                    now,
                    None if state == "queued" else now,
                    int(outcome == "deferred"),
                    delivery.queue_item_id,
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('last_check', ?)",
                (json.dumps(now),),
            )
            return True

    def deliveries(
        self, item_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Delivery], str | None]:
        """List attempts by start time and UUID, including expired attempts, for a known item."""
        scope = ["deliveries", item_id]
        after = ""
        parameters: list[Any] = [item_id]
        if cursor:
            position = cursor_decode(cursor, scope)
            if (
                not isinstance(position, list)
                or len(position) != 2
                or not _timestamp(position[0])
                or not isinstance(position[1], str)
            ):
                raise QueueError("invalid_cursor", "Invalid delivery cursor")
            after = " AND (started_at,id)>(?,?)"
            parameters.extend(position)
        with self.connection() as connection:
            self._get(connection, item_id)
            rows = connection.execute(
                "SELECT * FROM deliveries WHERE queue_item_id=?"
                + after
                + " ORDER BY started_at,id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        items = [
            Delivery(
                id=row["id"],
                queue_item_id=row["queue_item_id"],
                instruction=row["instruction"],
                started_at=row["started_at"],
                lease_expires_at=row["lease_expires_at"],
                sending_at=row["sending_at"],
                finished_at=row["finished_at"],
                outcome=row["outcome"],
                reason=row["reason"],
                receipt=row["receipt"],
            )
            for row in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = cursor_encode(scope, [items[-1].started_at, items[-1].id])
        return items, next_cursor

    def summary(self) -> QueueSummary:
        """Count the same visible open enrollments returned by page and the last dispatch check."""
        with self.connection() as connection:
            counts = dict(
                connection.execute(
                    "SELECT q.state,count(*) FROM queue_items q WHERE "
                    + OPEN_CONDITION
                    + " GROUP BY q.state"
                ).fetchall()
            )
            checked = connection.execute(
                "SELECT value FROM metadata WHERE key='last_check'"
            ).fetchone()
            return {
                "count": sum(counts.values()),
                "needs_attention": counts.get("changed", 0) + counts.get("failed", 0),
                "last_check": json.loads(checked[0]) if checked else None,
            }

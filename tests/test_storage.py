"""Crash, concurrency, ordering, and migration guarantees of the durable queue."""

import base64
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from carrie_on.legacy import LegacyEntry
from carrie_on.queue import EnqueueCommand, QueueError, QueueItem
from carrie_on.storage import QueueStore


class StorageBehavior(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "queue.sqlite3"
        self.store = QueueStore(self.path)
        self.store.initialize()
        self.item = QueueItem(
            id=str(uuid4()),
            chat_id="thread-a",
            title="Task A",
            account_id="account-a",
            limit_id="codex",
            anchor_message_id="original",
            model="model-a",
            instruction="Continue",
            reset_windows={"primary": 100},
            enrolled_while_busy=False,
            queued_at=50,
            updated_at=50,
        )
        self.command = self.command_for(self.item)
        self.store.enqueue(self.item, self.command)

    @staticmethod
    def command_for(item):
        return EnqueueCommand(id=item.id, chat_id=item.chat_id, instruction=item.instruction)

    def add(self, **changes):
        item = replace(self.item, **changes)
        self.store.enqueue(item, self.command_for(item))
        return item

    def legacy_entry(self, **changes):
        fields = {
            "id": str(uuid4()),
            "thread_id": self.item.chat_id,
            "title": self.item.title,
            "account_id": self.item.account_id,
            "limit_id": self.item.limit_id,
            "anchor_message_id": self.item.anchor_message_id,
            "model": self.item.model,
            "prompt": "Legacy instruction",
            "reset_windows": self.item.reset_windows,
            "enrolled_while_busy": False,
        }
        fields.update(changes)
        return LegacyEntry(**fields)

    def write_legacy(self, entries=(), history=()):
        path = self.path.with_name("resume-queue.json")
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "paused": True,
                    "entries": [asdict(entry) for entry in entries],
                    "history": list(history),
                }
            )
        )
        return path

    def test_concurrent_claims_create_a_single_active_delivery(self):
        with ThreadPoolExecutor(8) as pool:
            claims = list(pool.map(lambda _: self.store.claim(now=100), range(8)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_later_legacy_import_preserves_current_work_and_is_idempotent(self):
        legacy = self.legacy_entry()
        path = self.write_legacy([legacy])
        self.store.import_legacy_snapshot(path, "previous_import")
        self.store.import_legacy_snapshot(path, "previous_import")
        self.assertEqual(self.store.get(legacy.id).item.state, "cancelled")
        self.assertEqual([entry.item for entry in self.store.page()[0]], [self.item])
        self.assertEqual(len(self.store.page(state="all")[0]), 2)

    def test_expired_delivery_reclaims_and_old_worker_cannot_send_or_complete(self):
        _, first = self.store.claim(now=100)
        self.assertIsNone(self.store.claim(now=219))
        _, second = self.store.claim(now=220)
        self.assertNotEqual(first.id, second.id)
        self.assertFalse(self.store.begin_send(first, now=221))
        self.assertFalse(self.store.finish(first, "sent", "Late reply", now=221))
        self.assertTrue(self.store.begin_send(second, now=221))
        self.assertTrue(self.store.finish(second, "sent", "Accepted", now=222))
        self.assertEqual(
            [delivery.outcome for delivery in self.store.deliveries(self.item.id)[0]],
            ["expired", "sent"],
        )

    def test_process_death_after_claim_recovers_from_persisted_lease(self):
        script = "from carrie_on.storage import QueueStore; import os,sys; QueueStore(sys.argv[1]).claim(now=100); os._exit(17)"
        result = subprocess.run([sys.executable, "-c", script, str(self.path)], check=False)
        self.assertEqual(result.returncode, 17)
        self.assertIsNone(self.store.claim(now=219))
        self.assertEqual(self.store.claim(now=220)[0].id, self.item.id)

    def test_deferrals_order_independently_and_new_items_inherit_current_minimum(self):
        other = self.add(id=str(uuid4()), chat_id="thread-b", queued_at=51)
        for now, expected in (
            (100, self.item.id),
            (101, other.id),
            (102, self.item.id),
        ):
            item, delivery = self.store.claim(now=now)
            self.assertEqual(item.id, expected)
            self.store.finish(delivery, "deferred", "Busy", now=now)
        newest = self.add(id=str(uuid4()), chat_id="thread-c", queued_at=52)
        self.assertEqual(self.store.get(newest.id).item.deferrals, 1)
        self.assertEqual(self.store.claim(now=103)[0].id, other.id)

    def test_replay_survives_edit_completion_and_restart_without_resetting_content(
        self,
    ):
        edited = self.store.edit(self.item.id, "Revised", 1, now=60)
        self.assertEqual(self.store.replay(self.command), edited)
        self.store.cancel(self.item.id, now=70)
        restarted = QueueStore(self.path)
        restarted.initialize()
        entry, created = restarted.enqueue(self.item, self.command)
        self.assertFalse(created)
        self.assertEqual((entry.item.state, entry.item.instruction), ("cancelled", "Revised"))
        with self.assertRaisesRegex(QueueError, "different creation"):
            self.store.enqueue(self.item, replace(self.command, instruction="different"))

    def test_duplicate_chat_rejected_but_cancelled_chat_can_be_enrolled_again(self):
        replacement = replace(self.item, id=str(uuid4()), queued_at=60)
        with self.assertRaisesRegex(QueueError, "already queued"):
            self.store.enqueue(replacement, self.command_for(replacement))
        self.store.cancel(self.item.id, now=70)
        self.store.enqueue(replacement, self.command_for(replacement))
        self.assertEqual(len(self.store.page(state="all")[0]), 2)
        self.assertEqual(self.store.page()[0][0].item.id, replacement.id)

    def test_edits_and_removal_reject_active_delivery_but_not_other_items(self):
        other = self.add(id=str(uuid4()), chat_id="thread-b", queued_at=51)
        item, delivery = self.store.claim(now=100)
        for operation in (
            lambda: self.store.edit(item.id, "Changed", item.revision, now=101),
            lambda: self.store.cancel(item.id, now=101),
        ):
            with self.assertRaises(QueueError) as error:
                operation()
            self.assertEqual(error.exception.code, "delivery_active")
        self.assertEqual(self.store.edit(other.id, "Other", 1, now=101).item.instruction, "Other")
        self.store.finish(delivery, "deferred", "Busy", now=102)
        self.store.cancel(item.id, now=103)
        self.assertEqual(self.store.cancel(item.id, now=104).item.state, "cancelled")

    def test_stale_edit_cannot_overwrite_another_client(self):
        self.store.edit(self.item.id, "First", 1, now=100)
        with self.assertRaises(QueueError) as error:
            self.store.edit(self.item.id, "Second", 1, now=101)
        self.assertEqual(error.exception.code, "stale_revision")
        self.assertEqual(self.store.get(self.item.id).item.instruction, "First")

    def test_live_readers_and_rollback_leave_committed_state_intact(self):
        with (
            self.assertRaisesRegex(RuntimeError, "abort"),
            self.store.connection(write=True) as connection,
        ):
            connection.execute("UPDATE queue_items SET instruction='Uncommitted'")
            self.assertEqual(self.store.get(self.item.id).item.instruction, "Continue")
            raise RuntimeError("abort")
        self.assertEqual(self.store.get(self.item.id).item.instruction, "Continue")

    def test_pagination_is_query_bound_and_keeps_every_item(self):
        for index in range(4):
            self.add(id=str(uuid4()), chat_id=f"chat-{index}", queued_at=51 + index)
        entries, cursor = self.store.page(limit=2)
        self.assertIsNotNone(cursor)
        with self.assertRaises(QueueError):
            self.store.page(state="all", cursor=cursor)
        ids = [entry.item.id for entry in entries]
        while cursor:
            page, cursor = self.store.page(limit=2, cursor=cursor)
            ids.extend(entry.item.id for entry in page)
        self.assertEqual(len(set(ids)), 5)

    def test_queue_and_delivery_cursors_reject_overflow_and_nonfinite_values(self):
        for value in (2**100, -(2**100), float("inf"), float("nan")):
            scopes = (["queue", "open", None], ["deliveries", self.item.id])
            positions = ([0, value, self.item.id], [value, self.item.id])
            for scope, position in zip(scopes, positions):
                cursor = base64.urlsafe_b64encode(json.dumps([scope, position]).encode()).decode()
                operation = (
                    self.store.page
                    if scope[0] == "queue"
                    else lambda **kwargs: self.store.deliveries(self.item.id, **kwargs)
                )
                with (
                    self.subTest(scope=scope[0], value=value),
                    self.assertRaises(QueueError) as error,
                ):
                    operation(cursor=cursor)
                self.assertEqual(error.exception.code, "invalid_cursor")
        cursor = base64.urlsafe_b64encode(
            json.dumps([["queue", "open", None], [2**100, 50, "id"]]).encode()
        ).decode()
        with self.assertRaises(QueueError):
            self.store.page(cursor=cursor)

    def test_database_is_private_and_rejects_future_schema_without_overwriting(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            connection.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(ValueError, "newer"):
            self.store.initialize()

    def test_schema_upgrade_preserves_queue_and_delivery_and_allows_nul_instruction(
        self,
    ):
        _, delivery = self.store.claim(now=100)
        self.store.begin_send(delivery, now=101)
        with self.store.connection() as connection:
            connection.execute("PRAGMA user_version=1")
        self.store.initialize()
        entry = self.store.get(self.item.id)
        self.assertEqual(entry.active_delivery.sending_at, 101)
        self.assertEqual(self.store.deliveries(self.item.id)[0][0].id, delivery.id)
        self.store.finish(delivery, "deferred", "Busy", now=102)
        self.assertEqual(
            self.store.edit(self.item.id, "\x00Keep exact text", 4, now=103).item.instruction,
            "\x00Keep exact text",
        )

    def test_legacy_json_is_imported_once_with_uncertain_delivery_and_without_pause(
        self,
    ):
        entry = self.legacy_entry(state="sending", attempted_at=70)
        legacy = self.write_legacy([entry])
        database = QueueStore(self.path.with_name("import.sqlite3"))
        database.initialize(legacy)
        item = database.get(entry.id).item
        self.assertEqual((item.state, item.instruction), ("queued", "Legacy instruction"))
        self.assertEqual(database.deliveries(item.id)[0][0].outcome, "expired")
        database.cancel(item.id)
        database.initialize(legacy)
        self.assertEqual(database.get(item.id).item.state, "cancelled")
        self.assertTrue(legacy.exists())
        with database.connection() as connection:
            self.assertNotIn(
                "paused",
                [row[1] for row in connection.execute("PRAGMA table_info(queue_items)")],
            )

    def test_active_and_terminal_legacy_nul_text_and_partial_history_survive_restart(
        self,
    ):
        active = self.legacy_entry(prompt="\x00Active text")
        sent = self.legacy_entry(
            thread_id="sent-chat",
            prompt="\x00Sent text",
            state="sending",
            attempted_at=80,
        )
        history = [
            {**asdict(sent), "outcome": "resumed", "finished_at": 90},
            {"id": "partial", "outcome": "resumed", "extra": {"preserve": True}},
            "opaque",
        ]
        legacy = self.write_legacy([active], history)
        database = QueueStore(self.path.with_name("import.sqlite3"))
        database.initialize(legacy)
        for _ in range(2):
            database = QueueStore(database.path)
            database.initialize(legacy)
            database.import_legacy_snapshot(legacy, "recovery")
        self.assertEqual(database.get(active.id).item.instruction, "\x00Active text")
        self.assertEqual(
            (
                database.get(sent.id).item.instruction,
                database.get(sent.id).item.state,
                database.get(sent.id).item.finished_at,
            ),
            ("\x00Sent text", "sent", 90),
        )
        deliveries = database.deliveries(sent.id)[0]
        self.assertEqual(
            [(d.started_at, d.sending_at, d.finished_at, d.outcome) for d in deliveries],
            [(80, 80, 90, "sent")],
        )
        with database.connection() as connection:
            opaque = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT value FROM metadata WHERE key LIKE 'legacy_history:%'"
                )
            ]
        self.assertCountEqual(opaque, history[1:])
        self.assertEqual(len(database.page(state="all")[0]), 2)

    def test_queued_item_remains_visible_when_newer_terminal_history_exists(self):
        legacy = self.legacy_entry()
        self.store.import_legacy_snapshot(self.write_legacy([legacy]), "recovery")
        with self.store.connection(write=True) as connection:
            connection.execute("UPDATE queue_items SET queued_at=100 WHERE id=?", (legacy.id,))
        self.assertEqual([entry.item.id for entry in self.store.page()[0]], [self.item.id])
        self.assertEqual(self.store.summary()["count"], 1)

    def test_bad_legacy_import_rolls_back_and_keeps_original_file(self):
        path = self.path.with_name("bad.json")
        path.write_text('{"version": 99}')
        database = QueueStore(self.path.with_name("new.sqlite3"))
        with self.assertRaises(ValueError):
            database.initialize(path)
        self.assertEqual(path.read_text(), '{"version": 99}')
        with closing(sqlite3.connect(database.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)

"""Test SQLite persistence, concurrent writes, schema upgrades, and JSON imports."""

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
from unittest.mock import patch
from uuid import UUID, uuid4

from yoyo import get_backend, read_migrations

from carrie_on.legacy import LegacyEntry, LegacyImporter
from carrie_on.ports import StorageUnavailable
from carrie_on.queue import QueueError
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance
from tests.fixtures import instant, queue_entry


class StorageBehavior(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "queue.sqlite3"
        self.maintenance = SqliteStorageMaintenance(self.path)
        self.maintenance.initialize()
        self.now = 100
        self.store = SqliteQueueStorage(self.path, clock=lambda: instant(self.now))
        self.entry = queue_entry()
        self.store.create_entry(self.entry)

    def add(self, **changes):
        entry = replace(self.entry, id=uuid4(), chat_id=uuid4(), **changes)
        self.store.create_entry(entry)
        return entry

    def legacy_entry(self, **changes):
        fields = dict(
            id=str(uuid4()),
            thread_id=str(self.entry.chat_id),
            title=self.entry.title,
            account_id=self.entry.account_id,
            limit_id=self.entry.limit_id,
            anchor_message_id=self.entry.anchor_message_id,
            model=self.entry.model,
            prompt="Legacy instruction",
            reset_windows=dict(self.entry.reset_windows),
            enrolled_while_busy=False,
        )
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

    def imported_storage(self, path):
        database = self.path.with_name("import.sqlite3")
        SqliteStorageMaintenance(database).initialize()
        store = SqliteQueueStorage(database)
        LegacyImporter(store).import_file(path)
        return store

    def test_concurrent_attempt_creation_acquires_each_entry_once(self):
        other = self.add(queued_at=instant(51))
        with ThreadPoolExecutor(8) as pool:
            attempts = list(pool.map(lambda _: self.store.create_delivery_attempt(), range(8)))
        self.assertCountEqual([a.entry.id for a in attempts if a], [self.entry.id, other.id])
        self.assertEqual(self.store.get_queue_summary().dispatching, 2)

    def test_failure_retains_attempt_until_expiry_and_next_entry_is_available(self):
        other = self.add(queued_at=instant(51))
        first = self.store.create_delivery_attempt()
        failed = self.store.update_entry_state(self.entry.id, "failed", error="Busy")
        self.assertTrue(failed.dispatching)
        self.assertEqual(self.store.create_delivery_attempt().entry.id, other.id)
        self.now = 219
        self.assertIsNone(self.store.create_delivery_attempt())
        self.now = 220
        retry = self.store.create_delivery_attempt()
        self.assertEqual(retry.entry.id, self.entry.id)
        self.assertNotEqual(retry.id, first.id)
        self.assertEqual(retry.entry.error, "Busy")

    def test_process_death_after_attempt_creation_recovers_from_persisted_expiry(self):
        script = """from carrie_on.storage import SqliteQueueStorage
from datetime import datetime, timezone
from pathlib import Path
import os, sys
SqliteQueueStorage(Path(sys.argv[1]), clock=lambda: datetime.fromtimestamp(100, timezone.utc)).create_delivery_attempt()
os._exit(17)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.path)], check=False)
        self.assertEqual(result.returncode, 17)
        self.now = 219
        self.assertIsNone(self.store.create_delivery_attempt())
        self.now = 220
        self.assertEqual(self.store.create_delivery_attempt().entry.id, self.entry.id)

    def test_duplicate_insert_cannot_overwrite_edited_or_terminal_entries(self):
        self.store.update_instructions(self.entry.id, "\x00Revised ☃", revision=1)
        self.store.update_entry_state(self.entry.id, "cancelled")
        self.maintenance.initialize()
        restarted = SqliteQueueStorage(self.path)
        with self.assertRaises(QueueError) as error:
            restarted.create_entry(self.entry)
        self.assertEqual(error.exception.code, "entry_exists")
        stored = restarted.get_entry(self.entry.id)
        self.assertEqual((stored.state, stored.instructions), ("cancelled", "\x00Revised ☃"))

    def test_unique_pending_chat_allows_reenrollment_after_terminal_state(self):
        replacement = replace(self.entry, id=uuid4())
        self.store.update_entry_state(self.entry.id, "failed", error="Retry")
        with self.assertRaisesRegex(QueueError, "already queued"):
            self.store.create_entry(replacement)
        self.store.update_entry_state(self.entry.id, "stale")
        self.store.create_entry(replacement)
        self.assertEqual(len(self.store.list_entries(states=None)[0]), 2)
        self.assertEqual(self.store.list_entries()[0][0].id, replacement.id)

    def test_edits_and_removal_reject_active_attempt_but_not_other_entries(self):
        other = self.add(queued_at=instant(51))
        self.store.create_delivery_attempt()
        for operation in (
            lambda: self.store.update_instructions(self.entry.id, "Changed", revision=1),
            lambda: self.store.update_entry_state(self.entry.id, "cancelled"),
        ):
            with self.assertRaises(QueueError) as error:
                operation()
            self.assertEqual(error.exception.code, "delivery_active")
        self.assertEqual(
            self.store.update_instructions(other.id, "Other", revision=1).instructions, "Other"
        )
        self.now = 220
        removed = self.store.update_entry_state(self.entry.id, "cancelled")
        self.assertEqual(self.store.update_entry_state(self.entry.id, "cancelled"), removed)

    def test_revision_check_is_atomic_under_competing_edits(self):
        def edit(text):
            try:
                return self.store.update_instructions(self.entry.id, text, revision=1).instructions
            except QueueError as error:
                return error.code

        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(edit, ["first", "second"]))
        self.assertEqual(outcomes.count("stale_revision"), 1)
        self.assertIn(self.store.get_entry(self.entry.id).instructions, outcomes)

    def test_state_summary_and_terminal_entries_never_retry(self):
        sent = self.add()
        stale = self.add()
        cancelled = self.add()
        self.store.update_entry_state(sent.id, "sent")
        self.store.update_entry_state(stale.id, "stale")
        self.store.update_entry_state(cancelled.id, "cancelled", error="Terminal failure")
        self.store.update_entry_state(self.entry.id, "failed", error="Retry")
        summary = self.store.get_queue_summary()
        self.assertEqual(summary.count, 1)
        self.assertEqual(summary.needs_attention, 2)
        self.assertEqual(
            summary.counts, {"queued": 0, "failed": 1, "stale": 1, "cancelled": 1, "sent": 1}
        )
        self.assertEqual(self.store.create_delivery_attempt().entry.id, self.entry.id)
        self.assertIsNone(self.store.create_delivery_attempt())
        self.store.update_entry_state(self.entry.id, "sent")
        self.assertEqual(self.store.get_entry(self.entry.id).error, "")

    def test_interrupted_import_resumes_without_overwriting_or_cancelling_its_entries(self):
        historical = queue_entry(chat_id=uuid4(), state="sent", finished_at=instant(60))
        pending = replace(historical, id=uuid4(), state="queued", finished_at=None)
        insert = self.store.create_entry

        def interrupted(entry):
            if entry.id == pending.id:
                raise StorageUnavailable("Interrupted import")
            return insert(entry)

        importer = LegacyImporter(self.store)
        with patch.object(self.store, "create_entry", side_effect=interrupted):
            with self.assertRaisesRegex(StorageUnavailable, "Interrupted import"):
                importer.import_records([historical, pending])
        self.assertEqual(self.store.get_entry(historical.id).state, "sent")
        self.assertIsNone(self.store.get_entry(pending.id))
        self.assertTrue(importer.import_records([historical, pending]))
        self.assertEqual(self.store.get_entry(pending.id).state, "queued")
        self.store.update_instructions(pending.id, "Edited after import", revision=1)
        self.assertFalse(importer.import_records([historical, pending]))
        self.assertEqual(self.store.get_entry(pending.id).instructions, "Edited after import")

    def test_query_bound_pagination_keeps_all_entries_with_equal_timestamps(self):
        for _ in range(4):
            self.add()
        entries, cursor = self.store.list_entries(limit=2)
        with self.assertRaises(QueueError):
            self.store.list_entries(states=None, cursor=cursor)
        ids = [entry.id for entry in entries]
        while cursor:
            entries, cursor = self.store.list_entries(limit=2, cursor=cursor)
            ids.extend(entry.id for entry in entries)
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual(ids, sorted(ids))

    def test_malformed_cursors_fail_before_binding(self):
        for value in (2**100, -(2**100), 10**1000, float("inf"), float("nan"), "bad", {}, True):
            cursor = base64.urlsafe_b64encode(
                json.dumps(
                    [["queue", ["queued", "failed"], None], [value, str(self.entry.id)]]
                ).encode()
            ).decode()
            with self.assertRaises(QueueError):
                self.store.list_entries(cursor=cursor)

    def test_migrations_use_the_exact_path_including_uri_characters(self):
        path = self.path.with_name("queue #1?100%.sqlite3")
        SqliteStorageMaintenance(path).initialize()
        storage = SqliteQueueStorage(path)
        storage.create_entry(self.entry)
        self.assertEqual(storage.get_entry(self.entry.id), self.entry)
        self.assertFalse(self.path.with_name("queue ").exists())

    def test_private_database_wal_and_backup(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        backup = self.path.with_name("backup.sqlite3")
        self.maintenance.create_backup(backup)
        self.assertEqual(SqliteQueueStorage(backup).get_entry(self.entry.id), self.entry)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_initialization_rejects_migrations_unknown_to_this_release(self):
        directory = self.path.with_name("migrations")
        directory.mkdir()
        (directory / "0002_future.sql").write_text("CREATE TABLE future_settings (id TEXT);")
        with get_backend("sqlite:///" + str(self.path)) as backend:
            with backend.lock():
                backend.apply_migrations(backend.to_apply(read_migrations(str(directory))))

        with self.assertRaisesRegex(StorageUnavailable, "migrations unknown to this release"):
            self.maintenance.initialize()
        self.assertEqual(self.store.get_entry(self.entry.id), self.entry)

    def test_reinitialization_preserves_entries_history_and_active_attempts(self):
        self.store.update_instructions(self.entry.id, "Edited\x00", revision=1)
        finished = self.add()
        self.store.update_entry_state(finished.id, "sent")
        attempt = self.store.create_delivery_attempt()
        self.maintenance.initialize()
        self.maintenance.initialize()
        saved = self.store.get_entry(self.entry.id)
        self.assertEqual((saved.instructions, saved.revision), ("Edited\x00", 2))
        self.assertTrue(saved.dispatching)
        self.assertEqual(self.store.get_entry(finished.id).state, "sent")
        self.assertIsNone(self.store.create_delivery_attempt())
        self.now = attempt.expires_at.timestamp()
        self.assertEqual(self.store.create_delivery_attempt().entry.id, self.entry.id)

    def test_external_import_preserves_current_work_and_is_idempotent(self):
        legacy = self.legacy_entry()
        path = self.write_legacy([legacy])
        importer = LegacyImporter(self.store)
        self.assertTrue(importer.import_file(path))
        self.assertFalse(importer.import_file(path))
        self.assertEqual(self.store.get_entry(UUID(legacy.id)).state, "cancelled")
        self.assertEqual(self.store.list_entries()[0], [self.entry])

    def test_json_import_preserves_uncertain_work_history_text_and_source(self):
        active = self.legacy_entry(prompt="\x00Active", state="sending", attempted_at=70)
        sent = self.legacy_entry(thread_id=str(uuid4()), prompt="\x00Sent")
        history = [{**asdict(sent), "outcome": "resumed", "finished_at": 90}, "opaque"]
        path = self.write_legacy([active], history)
        original = path.read_bytes()
        store = self.imported_storage(path)
        self.assertEqual(store.get_entry(UUID(active.id)).state, "queued")
        self.assertEqual(store.get_entry(UUID(active.id)).instructions, "\x00Active")
        self.assertEqual(store.get_entry(UUID(sent.id)).state, "sent")
        store.update_entry_state(UUID(active.id), "cancelled")
        self.imported_storage(path)
        self.assertEqual(store.get_entry(UUID(active.id)).state, "cancelled")
        self.assertEqual(path.read_bytes(), original)

    def test_turn_anchored_json_and_recorded_acceptance_import_without_network(self):
        active = self.legacy_entry(anchor_message_id=None, anchor_turn_id="turn-a")
        sent = self.legacy_entry(
            thread_id=str(uuid4()),
            anchor_message_id=None,
            anchor_turn_id="turn-b",
            accepted_turn_id="accepted",
        )
        store = self.imported_storage(self.write_legacy([active, sent]))
        self.assertEqual(store.get_entry(UUID(active.id)).anchor_turn_id, "turn-a")
        self.assertEqual(store.get_entry(UUID(sent.id)).state, "sent")

    def test_unreadable_history_does_not_block_pending_work(self):
        active = self.legacy_entry()
        historical = self.legacy_entry(thread_id=str(uuid4()))
        for record in (
            {"id": []},
            {**asdict(historical), "thread_id": []},
            {**asdict(historical), "finished_at": 10**400},
            {**asdict(historical), "finished_at": 253402300800},
        ):
            with self.subTest(record=record):
                path = self.write_legacy([active], [record])
                original = path.read_bytes()
                store = self.imported_storage(path)
                self.assertEqual(
                    store.get_entry(UUID(active.id)).instructions, "Legacy instruction"
                )
                self.assertIsNone(store.get_entry(UUID(historical.id)))
                self.assertEqual(path.read_bytes(), original)

    def test_bad_json_leaves_current_schema_and_original_file_intact(self):
        path = self.path.with_name("bad.json")
        path.write_text('{"version":99}')
        with self.assertRaises(ValueError):
            LegacyImporter(self.store).import_file(path)
        self.assertEqual(path.read_text(), '{"version":99}')
        self.assertEqual(self.store.get_entry(self.entry.id), self.entry)

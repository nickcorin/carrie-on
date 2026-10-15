"""First-launch migration verified through final SQLite state and recovery artifacts."""

import json
import plistlib
import shutil
import subprocess
import unittest
from dataclasses import asdict, replace
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from carrie_on.legacy import LegacyEntry
from carrie_on.macos import installation, legacy_migration
from carrie_on.macos.runtime import PROFILES
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance
from tests import test_distribution as fixtures
from tests.fixtures import instant


class MigrationTests(unittest.TestCase):
    run_process = fixtures.FirstLaunchTests.run_process
    args = fixtures.FirstLaunchTests.args

    def setUp(self):
        fixtures.FirstLaunchTests.setUp(self)
        self.previous = self.home / "Library/Application Support/Tokenmaxxer"
        self.previous.mkdir()
        self.migration = legacy_migration.LegacyMigration(
            self.home, self.data, self.runtime.profile.labels
        )
        (self.previous / "settings.json").write_text(
            json.dumps(
                {
                    "codex": str(self.codex),
                    "port": 8877,
                    "interval": 90,
                    "socket": "/custom/socket",
                    "codex_home": "/custom/codex",
                    "schema_version": 1,
                }
            )
        )
        lifecycle = self.previous / "Lifecycle"
        (lifecycle / "Agents").mkdir(parents=True)
        (lifecycle / "preferences.plist").write_bytes(plistlib.dumps({"launch_at_login": False}))
        (self.previous / ".runtime").mkdir()
        (self.previous / ".runtime/worker.log").write_text("previous diagnostics")
        for label in legacy_migration.PREVIOUS_LABELS:
            path = self.home / "Library/LaunchAgents" / f"{label}.plist"
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = plistlib.dumps({"Label": label, "ProgramArguments": ["/old/backend"]})
            path.write_bytes(payload)
            (lifecycle / "Agents" / path.name).write_bytes(payload)
            self.loaded.add(label)

    def setup_app(self, **overrides):
        return installation.setup(self.runtime, **self.args(**overrides))

    def database(self):
        SqliteStorageMaintenance(self.runtime.state).initialize()
        return SqliteQueueStorage(self.runtime.state)

    @staticmethod
    def entry(thread="chat-a", **changes):
        values = {
            "id": str(uuid4()),
            "thread_id": str(uuid5(NAMESPACE_URL, thread)),
            "title": thread,
            "account_id": "account",
            "limit_id": "codex",
            "anchor_message_id": "anchor",
            "model": "model",
            "prompt": "Continue",
            "reset_windows": {"primary": 123},
            "enrolled_while_busy": False,
        }
        values.update(changes)
        return LegacyEntry(**values)

    @staticmethod
    def queue(directory, entries, history=(), paused=False):
        (directory / "resume-queue.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "entries": [asdict(entry) for entry in entries],
                    "history": list(history),
                    "paused": paused,
                }
            )
        )

    def test_migrates_queue_settings_preferences_and_retires_old_installation(self):
        waiting = self.entry()
        sending = self.entry("chat-b", state="sending", attempted_at=100)
        partial = {"id": "finished", "outcome": "resumed"}
        self.queue(self.previous, [waiting, sending], [partial], paused=True)
        result = self.setup_app()
        self.assertEqual((result["port"], result["started"]), (8877, True))
        store = self.database()
        self.assertCountEqual(
            [(str(entry.id), entry.state) for entry in store.list_entries()[0]],
            [(waiting.id, "queued"), (sending.id, "queued")],
        )
        recovered = json.loads((self.migration.recovery / "retired/resume-queue.json").read_text())
        self.assertEqual(recovered["history"], [partial])
        self.assertEqual(recovered["entries"][1]["attempted_at"], 100)
        self.assertFalse((self.data / "resume-queue.json").exists())
        settings = json.loads((self.data / "settings.json").read_text())
        self.assertEqual(
            (settings["interval"], settings["socket"], settings["codex_home"]),
            (90, "/custom/socket", "/custom/codex"),
        )
        self.assertEqual(
            plistlib.loads((self.data / "Lifecycle/preferences.plist").read_bytes()),
            {"launch_at_login": False},
        )
        self.assertEqual(self.loaded, set(self.runtime.profile.labels))
        self.assertFalse(self.previous.exists())
        self.assertEqual(list((self.home / "Library/LaunchAgents").iterdir()), [])
        self.assertEqual(
            (self.migration.recovery / "retired/.runtime/worker.log").read_text(),
            "previous diagnostics",
        )
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "complete")
        self.restarts.clear()
        self.setup_app()
        self.assertEqual(self.restarts, [])
        self.assertEqual(len(self.database().list_entries()[0]), 2)

    def test_merges_existing_installation_and_never_resurrects_finished_entries(self):
        finished = self.entry("finished", state="sending", attempted_at=100)
        old = self.entry("old")
        duplicate = self.entry("shared", prompt="Older instruction")
        current = self.entry("shared", prompt="Current instruction")
        self.queue(self.previous, [finished, old, duplicate])
        self.queue(
            self.data,
            [current],
            [{**asdict(finished), "outcome": "resumed", "finished_at": 200}],
        )
        (self.data / "settings.json").write_text(
            json.dumps({"codex": str(self.codex), "port": 9000, "interval": 120})
        )
        result = self.setup_app()
        self.assertEqual(result["port"], 9000)
        store = self.database()
        self.assertCountEqual(
            [(entry.title, entry.instructions) for entry in store.list_entries()[0]],
            [("shared", "Current instruction"), ("old", "Continue")],
        )
        self.assertEqual(
            (
                store.get_entry(UUID(finished.id)).state,
                store.get_entry(UUID(finished.id)).finished_at,
            ),
            ("sent", instant(200)),
        )
        self.assertEqual(store.get_entry(UUID(duplicate.id)).state, "cancelled")
        self.setup_app()
        store = self.database()
        self.assertEqual(len(store.list_entries(states=None)[0]), 4)
        self.assertEqual(json.loads((self.data / "settings.json").read_text())["interval"], 120)

    def test_first_updater_queue_accepts_missing_presentation_lease_and_completes_once(
        self,
    ):
        entry = asdict(self.entry(prompt="Finish the queued task"))
        entry.pop("processing_until")
        (self.previous / "resume-queue.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "paused": False,
                    "entries": [entry],
                    "history": [],
                }
            )
        )
        self.setup_app()
        queue = self.database()
        attempt = queue.create_delivery_attempt()
        self.assertEqual(
            (str(attempt.entry.id), attempt.entry.instructions),
            (entry["id"], "Finish the queued task"),
        )
        queue.update_entry_state(attempt.entry.id, "sent")
        self.setup_app()
        queue = self.database()
        self.assertIsNone(queue.create_delivery_attempt())
        self.assertEqual(queue.get_entry(UUID(entry["id"])).state, "sent")

    def test_uncertain_send_dominates_waiting_duplicate_and_remains_visible_after_retry(
        self,
    ):
        waiting = self.entry()
        sending = self.entry(state="sending", attempted_at=100)
        self.queue(self.previous, [sending])
        self.queue(self.data, [waiting])
        self.setup_app()
        database = self.database()
        self.assertEqual([str(entry.id) for entry in database.list_entries()[0]], [sending.id])
        self.assertEqual(database.get_queue_summary().count, 1)
        self.assertEqual(database.get_entry(UUID(waiting.id)).state, "cancelled")
        self.setup_app()
        database = self.database()
        self.assertEqual([str(entry.id) for entry in database.list_entries()[0]], [sending.id])
        self.assertEqual(str(database.create_delivery_attempt().entry.id), sending.id)

    def test_held_enrollment_dominates_waiting_duplicate_and_stays_visible(self):
        waiting = self.entry()
        held = self.entry(
            state="needs_attention", attempted_at=100, note="Inspect previous attempt"
        )
        self.queue(self.previous, [held])
        self.queue(self.data, [waiting])
        with self.migration.prepare():
            store = self.database()
            self.assertEqual(
                [(str(entry.id), entry.state) for entry in store.list_entries()[0]],
                [(held.id, "failed")],
            )
            self.assertEqual(store.get_queue_summary().needs_attention, 1)
            self.assertEqual(str(store.create_delivery_attempt().entry.id), held.id)
        with self.migration.prepare():
            self.assertEqual(
                [str(entry.id) for entry in self.database().list_entries()[0]], [held.id]
            )

    def test_same_enrollment_keeps_attempt_state_without_duplicate_history(self):
        waiting = self.entry()
        sending = replace(waiting, state="sending", attempted_at=100)
        self.queue(self.previous, [sending])
        self.queue(self.data, [waiting])
        self.setup_app()
        store = self.database()
        self.assertEqual(
            [(str(entry.id), entry.state) for entry in store.list_entries(states=None)[0]],
            [(sending.id, "queued")],
        )

    def test_setup_failure_then_retry_keeps_progress_made_by_new_worker(self):
        entry = self.entry()
        self.queue(self.previous, [entry])
        self.fail_bootstrap = self.runtime.profile.labels[0]
        with self.assertRaises(subprocess.CalledProcessError):
            self.setup_app()
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "applied")
        self.assertTrue(self.previous.exists())
        queue = self.database()
        attempt = queue.create_delivery_attempt()
        queue.update_entry_state(attempt.entry.id, "sent")
        self.fail_bootstrap = None
        self.setup_app()
        queue = self.database()
        self.assertEqual(queue.get_entry(UUID(entry.id)).state, "sent")
        self.assertFalse(self.previous.exists())

    def test_interrupted_import_replays_the_saved_plan(self):
        entry = self.entry()
        self.queue(self.previous, [entry])
        write = legacy_migration.atomic_write

        def interrupted(path, payload):
            if path == self.migration.journal and json.loads(payload)["state"] == "applied":
                raise OSError("Interrupted before services started")
            return write(path, payload)

        with (
            patch.object(legacy_migration, "atomic_write", side_effect=interrupted),
            self.assertRaisesRegex(OSError, "Interrupted"),
        ):
            self.setup_app()
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "prepared")
        self.setup_app()
        self.assertEqual([str(item.id) for item in self.database().list_entries()[0]], [entry.id])
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "complete")

    def test_malformed_old_queue_is_preserved_without_blocking_setup(self):
        (self.previous / "resume-queue.json").write_text("interrupted JSON")
        result = self.setup_app()
        self.assertTrue(result["started"])
        self.assertEqual(self.database().list_entries()[0], [])
        self.assertEqual(
            (self.migration.recovery / "retired/resume-queue.json").read_text(),
            "interrupted JSON",
        )
        self.assertIn(
            "resume-queue.json",
            json.loads(self.migration.journal.read_text())["warnings"][0],
        )

    def test_dev_and_no_start_do_not_touch_old_installation(self):
        for profile, no_start in (
            (PROFILES["dev"], False),
            (PROFILES["release"], True),
        ):
            runtime = replace(self.runtime, profile=profile)
            (self.app / "Contents/Info.plist").write_bytes(
                plistlib.dumps({"CFBundleIdentifier": profile.identifier})
            )
            with (
                self.subTest(profile=profile.name),
                patch.object(installation, "configure", return_value={"started": not no_start}),
            ):
                result = installation.setup(runtime, **self.args(no_start=no_start))
                self.assertEqual(result["started"], not no_start)
                self.assertTrue((self.previous / "settings.json").exists())
                self.assertEqual(self.loaded, set(legacy_migration.PREVIOUS_LABELS))
                self.assertFalse(self.migration.journal.exists())

    def test_services_must_stop_before_any_data_is_imported(self):
        original = self.run_process

        def stuck(command, **kwargs):
            if command[:2] == ["/bin/launchctl", "print"]:
                return subprocess.CompletedProcess(command, 0)
            return original(command, **kwargs)

        with (
            patch("carrie_on.macos.legacy_migration.subprocess.run", side_effect=stuck),
            patch("carrie_on.macos.legacy_migration.time.sleep"),
            self.assertRaisesRegex(ValueError, "has not stopped"),
        ):
            self.setup_app()
        self.assertFalse((self.data / "settings.json").exists())
        self.assertTrue((self.previous / "settings.json").exists())

    def test_orphaned_bundle_services_are_removed_without_a_data_directory(self):
        shutil.rmtree(self.previous)
        path = self.migration.login_agent(legacy_migration.PREVIOUS_LABELS[0])
        path.write_bytes(
            plistlib.dumps(
                {
                    "ProgramArguments": [
                        "/Applications/Carrie On.app/Contents/MacOS/tokenmaxxer",
                        "tick",
                    ]
                }
            )
        )
        result = self.setup_app(codex=str(self.codex))
        self.assertTrue(result["started"])
        self.assertEqual(self.loaded, set(self.runtime.profile.labels))
        self.assertFalse(path.exists())
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "complete")

    def test_source_services_without_bundled_data_are_left_alone(self):
        shutil.rmtree(self.previous)
        result = self.setup_app(codex=str(self.codex))
        self.assertTrue(result["started"])
        self.assertEqual(
            self.loaded,
            set(self.runtime.profile.labels + legacy_migration.PREVIOUS_LABELS),
        )
        self.assertTrue(self.migration.login_agent(legacy_migration.PREVIOUS_LABELS[0]).exists())
        self.assertFalse(self.migration.journal.exists())

    def test_invalid_old_settings_fall_back_automatically(self):
        (self.previous / "settings.json").write_text(
            json.dumps({"codex": "/missing/cli", "port": -1, "interval": "slow"})
        )
        result = self.setup_app(codex=str(self.codex))
        self.assertEqual(result["port"], 8766)
        self.assertEqual(json.loads((self.data / "settings.json").read_text())["interval"], 60)
        self.assertEqual(len(json.loads(self.migration.journal.read_text())["warnings"]), 3)

    def test_restart_after_relocating_old_files_finishes_cleanup(self):
        entry = self.entry()
        self.queue(self.previous, [entry])
        write = legacy_migration.atomic_write

        def interrupted(path, payload):
            if path == self.migration.journal and json.loads(payload)["state"] == "complete":
                raise OSError("Interrupted after relocation")
            return write(path, payload)

        with (
            patch.object(legacy_migration, "atomic_write", side_effect=interrupted),
            self.assertRaisesRegex(OSError, "relocation"),
        ):
            self.setup_app()
        self.assertFalse(self.previous.exists())
        self.assertTrue((self.migration.recovery / "retired/settings.json").exists())
        self.setup_app()
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "complete")
        self.assertEqual([str(item.id) for item in self.database().list_entries()[0]], [entry.id])

    def test_cleanup_never_retires_an_installation_without_an_import_plan(self):
        self.migration.complete()
        self.assertTrue((self.previous / "settings.json").exists())
        self.assertTrue(self.migration.login_agent(legacy_migration.PREVIOUS_LABELS[0]).exists())
        self.setup_app()
        self.assertFalse(self.previous.exists())
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "complete")

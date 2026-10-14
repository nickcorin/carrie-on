"""First-launch migration with isolated homes and a simulated launchd session."""

from dataclasses import asdict
import json
import plistlib
import shutil
import subprocess
import unittest
from unittest.mock import patch
import uuid

import app_runtime
import bundle_install
import legacy_migration
from resume_queue import Entry, Queue, check_queue
import test_distribution as fixtures
from test_carrie_on import CodexFixture


class MigrationTests(unittest.TestCase):
    run_process = fixtures.FirstLaunchTests.run_process
    args = fixtures.FirstLaunchTests.args

    def setUp(self):
        fixtures.FirstLaunchTests.setUp(self)
        self.previous = self.home / "Library/Application Support/Tokenmaxxer"
        self.previous.mkdir()
        self.migration = legacy_migration.LegacyMigration(self.home, self.data, bundle_install.LABELS)
        (self.previous / "settings.json").write_text(json.dumps({
            "codex": str(self.codex), "port": 8877, "interval": 90,
            "socket": "/custom/socket", "codex_home": "/custom/codex", "schema_version": 1,
        }))
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

    def entry(self, thread="chat-a", **changes):
        values = dict(id=str(uuid.uuid4()), thread_id=thread, title=thread, account_id="account",
                      limit_id="codex", anchor_message_id="anchor", model="model", prompt="Continue",
                      reset_windows={"primary": 123}, enrolled_while_busy=False)
        values.update(changes)
        return Entry(**values)

    def queue(self, directory, entries, history=None, paused=False):
        queue = Queue(directory / "resume-queue.json")
        queue.entries = entries
        queue.data["paused"] = paused
        queue.data["history"] = history or []
        queue.save()
        return queue

    def test_migrates_queue_settings_preferences_and_retires_old_installation(self):
        waiting = self.entry()
        sending = self.entry("chat-b", state="sending", attempted_at=100)
        self.queue(self.previous, [waiting, sending], [{"id": "finished", "outcome": "resumed"}], paused=True)
        result = bundle_install.setup(self.args())
        self.assertEqual((result["port"], result["started"]), (8877, True))
        queue = Queue(self.data / "resume-queue.json")
        self.assertEqual([asdict(entry) for entry in queue.entries], [asdict(waiting), asdict(sending)])
        self.assertTrue(queue.data["paused"])
        self.assertEqual(queue.data["history"], [{"id": "finished", "outcome": "resumed"}])
        settings = json.loads((self.data / "settings.json").read_text())
        self.assertEqual((settings["interval"], settings["socket"], settings["codex_home"]),
                         (90, "/custom/socket", "/custom/codex"))
        self.assertEqual(plistlib.loads((self.data / "Lifecycle/preferences.plist").read_bytes()), {"launch_at_login": False})
        self.assertEqual(self.loaded, set(bundle_install.LABELS))
        self.assertFalse(self.previous.exists())
        self.assertEqual(list((self.home / "Library/LaunchAgents").iterdir()), [])
        self.assertEqual((self.migration.recovery / "retired/.runtime/worker.log").read_text(), "previous diagnostics")
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "complete")
        self.restarts.clear()
        bundle_install.setup(self.args())
        self.assertEqual(self.restarts, [])
        self.assertEqual(len(Queue(self.data / "resume-queue.json").entries), 2)

    def test_merges_existing_installation_and_never_resurrects_finished_entries(self):
        finished = self.entry("finished")
        old = self.entry("old")
        duplicate = self.entry("shared", prompt="Older instruction")
        current = self.entry("shared", prompt="Current instruction")
        self.queue(self.previous, [finished, old, duplicate])
        self.queue(self.data, [current], [{**asdict(finished), "outcome": "resumed", "finished_at": 200}])
        (self.data / "settings.json").write_text(json.dumps({"codex": str(self.codex), "port": 9000, "interval": 120}))
        result = bundle_install.setup(self.args())
        self.assertEqual(result["port"], 9000)
        queue = Queue(self.data / "resume-queue.json")
        self.assertEqual([(e.thread_id, e.prompt) for e in queue.entries], [("shared", "Current instruction"), ("old", "Continue")])
        self.assertEqual([h["outcome"] for h in queue.data["history"]], ["resumed", "superseded_by_migration"])
        self.assertEqual(json.loads((self.data / "settings.json").read_text())["interval"], 120)

    def test_first_updater_queue_resumes_once_after_migration_and_restart(self):
        entry = asdict(self.entry(
            "thread-a", id="8e2f700b-050f-4210-8503-103a2d383713", account_id="account-a",
            anchor_message_id="original", model="model-a", reset_windows={"primary": 100},
            prompt="Finish the queued task",
        ))
        # The first updater release predates the worker's presentation lease.
        entry.pop("processing_until")
        (self.previous / "resume-queue.json").write_text(json.dumps({
            "version": 2, "paused": False, "entries": [entry], "history": [],
        }))
        bundle_install.setup(self.args())
        codex = CodexFixture()
        check_queue(Queue(self.data / "resume-queue.json"), codex, codex, now=150)
        bundle_install.setup(self.args())
        queue = Queue(self.data / "resume-queue.json")
        check_queue(queue, codex, codex, now=151)
        self.assertEqual(codex.delivered, [{
            "thread_id": "thread-a", "prompt": "Finish the queued task",
            "message_id": "8e2f700b-050f-4210-8503-103a2d383713",
        }])
        self.assertEqual(queue.entries, [])
        self.assertEqual([event["outcome"] for event in queue.data["history"]], ["resumed"])

    def test_uncertain_send_dominates_waiting_duplicate_and_is_not_resent(self):
        waiting = self.entry()
        sending = self.entry(state="sending", attempted_at=100)
        self.queue(self.previous, [sending])
        self.queue(self.data, [waiting])
        bundle_install.setup(self.args())
        queue = Queue(self.data / "resume-queue.json")
        self.assertEqual((queue.entries[0].id, queue.entries[0].state), (sending.id, "sending"))
        class Desktop:
            def snapshot(self, thread):
                raise ValueError("Offline")
            def continue_chat(self, *args):
                raise AssertionError("Migration replayed an uncertain send")
        check_queue(queue, None, Desktop(), now=300)
        self.assertEqual(queue.entries[0].state, "needs_attention")
        self.assertEqual(queue.entries[0].attempted_at, 100)

    def test_same_enrollment_keeps_attempt_state_without_duplicate_history(self):
        waiting = self.entry()
        sending = self.entry(id=waiting.id, state="sending", attempted_at=100)
        self.queue(self.previous, [sending])
        self.queue(self.data, [waiting])
        bundle_install.setup(self.args())
        queue = Queue(self.data / "resume-queue.json")
        self.assertEqual([e.state for e in queue.entries], ["sending"])
        self.assertEqual(queue.data["history"], [])

    def test_setup_failure_then_retry_keeps_progress_made_by_new_worker(self):
        entry = self.entry()
        self.queue(self.previous, [entry])
        self.fail_bootstrap = bundle_install.carrie_on_ui.LABEL
        with self.assertRaises(subprocess.CalledProcessError):
            bundle_install.setup(self.args())
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "applied")
        self.assertTrue(self.previous.exists())
        queue = Queue(self.data / "resume-queue.json")
        queue.finish(queue.entries[0], "resumed", 300)
        self.fail_bootstrap = None
        bundle_install.setup(self.args())
        queue = Queue(self.data / "resume-queue.json")
        self.assertEqual(queue.entries, [])
        self.assertEqual(queue.data["history"][0]["outcome"], "resumed")
        self.assertFalse(self.previous.exists())

    def test_interrupted_import_replays_the_saved_plan(self):
        entry = self.entry()
        self.queue(self.previous, [entry])
        write = legacy_migration.atomic_write
        def interrupted(path, payload):
            if path == self.migration.journal and json.loads(payload)["state"] == "applied":
                raise OSError("Interrupted before services started")
            return write(path, payload)
        with patch.object(legacy_migration, "atomic_write", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "Interrupted"):
                bundle_install.setup(self.args())
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "prepared")
        bundle_install.setup(self.args())
        self.assertEqual([e.id for e in Queue(self.data / "resume-queue.json").entries], [entry.id])
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "complete")

    def test_malformed_old_queue_is_preserved_without_blocking_setup(self):
        (self.previous / "resume-queue.json").write_text("interrupted JSON")
        result = bundle_install.setup(self.args())
        self.assertTrue(result["started"])
        self.assertEqual(Queue(self.data / "resume-queue.json").entries, [])
        self.assertEqual((self.migration.recovery / "retired/resume-queue.json").read_text(), "interrupted JSON")
        self.assertIn("resume-queue.json", json.loads(self.migration.journal.read_text())["warnings"][0])

    def test_dev_and_no_start_do_not_touch_old_installation(self):
        for profile, no_start in ((app_runtime.PROFILES["dev"], False), (app_runtime.PROFILES["release"], True)):
            with self.subTest(profile=profile.name), patch.object(bundle_install, "PROFILE", profile), \
                 patch.object(bundle_install, "configure", return_value={"started": not no_start}):
                result = bundle_install.setup(self.args(no_start=no_start))
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
        with patch("legacy_migration.subprocess.run", side_effect=stuck), patch("legacy_migration.time.sleep"):
            with self.assertRaisesRegex(ValueError, "has not stopped"):
                bundle_install.setup(self.args())
        self.assertFalse((self.data / "settings.json").exists())
        self.assertTrue((self.previous / "settings.json").exists())

    def test_orphaned_bundle_services_are_removed_without_a_data_directory(self):
        shutil.rmtree(self.previous)
        path = self.migration.login_agent(legacy_migration.PREVIOUS_LABELS[0])
        path.write_bytes(plistlib.dumps({"ProgramArguments": ["/Applications/Carrie On.app/Contents/MacOS/tokenmaxxer", "tick"]}))
        result = bundle_install.setup(self.args(codex=str(self.codex)))
        self.assertTrue(result["started"])
        self.assertEqual(self.loaded, set(bundle_install.LABELS))
        self.assertFalse(path.exists())
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "complete")

    def test_source_services_without_bundled_data_are_left_alone(self):
        shutil.rmtree(self.previous)
        result = bundle_install.setup(self.args(codex=str(self.codex)))
        self.assertTrue(result["started"])
        self.assertEqual(self.loaded, set(bundle_install.LABELS + legacy_migration.PREVIOUS_LABELS))
        self.assertTrue(self.migration.login_agent(legacy_migration.PREVIOUS_LABELS[0]).exists())
        self.assertFalse(self.migration.journal.exists())

    def test_invalid_old_settings_fall_back_automatically(self):
        (self.previous / "settings.json").write_text(json.dumps({"codex": "/missing/cli", "port": -1, "interval": "slow"}))
        result = bundle_install.setup(self.args(codex=str(self.codex)))
        self.assertEqual(result["port"], 8766)
        self.assertEqual(json.loads((self.data / "settings.json").read_text())["interval"], 60)
        self.assertEqual(len(json.loads(self.migration.journal.read_text())["warnings"]), 3)

    def test_restart_after_relocating_old_files_finishes_cleanup(self):
        self.queue(self.previous, [self.entry()])
        write = legacy_migration.atomic_write
        def interrupted(path, payload):
            if path == self.migration.journal and json.loads(payload)["state"] == "complete":
                raise OSError("Interrupted after relocation")
            return write(path, payload)
        with patch.object(legacy_migration, "atomic_write", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "relocation"):
                bundle_install.setup(self.args())
        self.assertFalse(self.previous.exists())
        self.assertTrue((self.migration.recovery / "retired/settings.json").exists())
        bundle_install.setup(self.args())
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "complete")
        self.assertEqual(len(Queue(self.data / "resume-queue.json").entries), 1)

    def test_cleanup_never_retires_an_installation_without_an_import_plan(self):
        self.migration.complete()
        self.assertTrue((self.previous / "settings.json").exists())
        self.assertTrue(self.migration.login_agent(legacy_migration.PREVIOUS_LABELS[0]).exists())
        bundle_install.setup(self.args())
        self.assertFalse(self.previous.exists())
        self.assertEqual(json.loads(self.migration.journal.read_text())["state"], "complete")

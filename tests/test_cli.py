"""Run the real CLI through HTTP against an isolated server and synthetic Codex."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import uvicorn

from carrie_on.codex.client import CodexConnections
from carrie_on.macos.lifecycle import control_service
from carrie_on.macos.runtime import PROFILES
from carrie_on.server import create_application
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance
from tests.fixtures import CHAT_ID, LocalCodex


class CliContract(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.store = SqliteQueueStorage(self.home / "queue.sqlite3")
        for name in ("AppServer", "Desktop"):
            mock = patch("carrie_on.codex.client." + name, LocalCodex)
            mock.start()
            self.addCleanup(mock.stop)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        application = create_application(
            self.store,
            CodexConnections("fixture"),
            maintenance=SqliteStorageMaintenance(self.home / "queue.sqlite3"),
            port=port,
            run_worker=False,
        )
        self.service = application.state.service
        self.server = uvicorn.Server(uvicorn.Config(application, log_level="critical"))
        thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()

        def stop():
            self.server.should_exit = True
            thread.join(timeout=5)
            listener.close()
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not self.server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                self.fail("Fixture server failed to start")
            time.sleep(0.01)

    def cli(self, *args, stdin=None, code=0, timeout=10):
        result = subprocess.run(
            [sys.executable, "-m", "carrie_on", "--server", self.url, *args],
            env=dict(os.environ, HOME=str(self.home), NO_COLOR="1"),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        self.assertEqual(result.returncode, code, result.stderr + result.stdout)
        return result

    def test_help_version_and_removed_commands(self):
        self.assertIn("queue", self.cli("--help").stdout)
        self.assertIn("queue", self.cli("-h").stdout)
        self.assertIn("--instruction-file", self.cli("queue", "add", "-h").stdout)
        self.assertTrue(self.cli("--version").stdout.startswith("carrie-on "))
        self.assertIn("--instruction-file", self.cli("queue", "add", "--help").stdout)
        self.assertEqual(self.cli("pause", code=2).stdout, "")
        self.assertIn("No such command", self.cli("tick", code=2).stderr)

    def test_add_stdin_edit_cancel_and_history(self):
        item_id = str(uuid4())
        text = "Finish café tests\nKeep this line.\n"
        created = json.loads(
            self.cli(
                "--json",
                "queue",
                "add",
                CHAT_ID,
                "--id",
                item_id,
                "--instruction-file",
                "-",
                stdin=text,
            ).stdout
        )
        self.assertEqual(created["instruction"], text)
        self.assertEqual(created["id"], item_id)
        self.assertEqual(
            json.loads(self.cli("--json", "queue", "list").stdout)["items"][0]["id"],
            item_id,
        )
        instruction_file = self.home / "instruction with spaces.txt"
        instruction_file.write_text("A café instruction\nwith two lines")
        self.cli("queue", "edit", item_id, "--instruction-file", str(instruction_file))
        self.assertEqual(
            self.store.get_entry(UUID(item_id)).instructions, instruction_file.read_text()
        )
        self.cli("queue", "edit", item_id, "--instruction=--finish the tests")
        self.assertEqual(self.store.get_entry(UUID(item_id)).instructions, "--finish the tests")
        self.cli("queue", "cancel", item_id)
        self.cli("queue", "cancel", item_id)
        history = json.loads(self.cli("--json", "queue", "list", "--history").stdout)
        self.assertEqual(history["items"][0]["state"], "cancelled")
        self.assertIn("No such command", self.cli("queue", "deliveries", item_id, code=2).stderr)

    def test_edit_conflicts_preserve_saved_text_and_report_exit_4(self):
        created = json.loads(self.cli("--json", "queue", "add", CHAT_ID).stdout)
        item_id = created["id"]
        read_entry = self.store.get_entry

        def read_before_competing_edit(entry_id):
            entry = read_entry(entry_id)
            self.store.update_instructions(
                entry_id, "Saved by another client", revision=entry.revision
            )
            return entry

        with patch.object(self.store, "get_entry", side_effect=read_before_competing_edit):
            conflict = self.cli("queue", "edit", item_id, "--instruction", "Stale edit", code=4)
        self.assertEqual(conflict.stdout, "")
        self.assertIn("The entry changed", conflict.stderr)
        saved = self.store.get_entry(UUID(item_id))
        self.assertEqual(saved.instructions, "Saved by another client")
        self.assertEqual(saved.revision, 2)

        self.store.create_delivery_attempt()
        blocked = self.cli("queue", "edit", item_id, "--instruction", "Busy edit", code=4)
        self.assertEqual(blocked.stdout, "")
        self.assertIn("A delivery is active", blocked.stderr)
        self.assertEqual(
            self.store.get_entry(UUID(item_id)).instructions, "Saved by another client"
        )
        self.assertEqual(self.store.get_entry(UUID(item_id)).revision, 2)

    def test_exit_codes_and_stderr_are_scriptable(self):
        self.assertEqual(self.cli("queue", "show", str(uuid4()), code=3).stdout, "")
        self.assertEqual(self.cli("queue", "show", "bad-id", code=2).stdout, "")
        self.cli("queue", "add", CHAT_ID)
        self.assertEqual(self.cli("queue", "add", CHAT_ID, code=4).stdout, "")
        self.cli("queue", "add", CHAT_ID, "--instruction", " ", code=2)
        self.cli(
            "queue",
            "add",
            CHAT_ID,
            "--instruction",
            "x",
            "--instruction-file",
            "-",
            stdin="y",
            code=2,
        )

    def test_lost_create_reply_can_retry_same_id_without_duplication(self):
        item_id = str(uuid4())
        enqueue = self.service.enqueue

        def slow(*args, **kwargs):
            result = enqueue(*args, **kwargs)
            time.sleep(0.4)
            return result

        with patch.object(self.service, "enqueue", side_effect=slow):
            result = self.cli("--timeout", "0.1", "queue", "add", CHAT_ID, "--id", item_id, code=5)
        self.assertIn("outcome is unknown", result.stderr)
        self.assertIn(item_id, result.stderr)
        result = self.cli("--json", "queue", "add", CHAT_ID, "--id", item_id)
        self.assertEqual(json.loads(result.stdout)["id"], item_id)
        self.assertEqual(len(self.store.list_entries(states=None)[0]), 1)

    def test_interrupted_creation_discloses_id_and_replays_original_request(self):
        committed, release = threading.Event(), threading.Event()
        enqueue = self.service.enqueue

        def delayed_reply(*args, **kwargs):
            result = enqueue(*args, **kwargs)
            committed.set()
            release.wait(5)
            return result

        with patch.object(self.service, "enqueue", side_effect=delayed_reply):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "carrie_on",
                    "--server",
                    self.url,
                    "--json",
                    "queue",
                    "add",
                    CHAT_ID,
                    "--instruction",
                    "Keep this exact text",
                ],
                env=dict(os.environ, HOME=str(self.home)),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertTrue(committed.wait(5))
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130, stderr)
                self.assertEqual(stdout, "")
                self.assertNotIn("Traceback", stderr)
                item_id = stderr.split("Queue item ID: ", 1)[1].splitlines()[0]
                self.assertIn("outcome is unknown", stderr)
                self.assertIn("--id " + item_id, stderr)
            finally:
                release.set()
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.store.update_entry_state(UUID(item_id), "cancelled")
        replay = self.cli(
            "--json",
            "queue",
            "add",
            CHAT_ID,
            "--id",
            item_id,
            "--instruction",
            "Keep this exact text",
        )
        self.assertEqual(json.loads(replay.stdout)["state"], "cancelled")
        self.assertEqual(
            [str(entry.id) for entry in self.store.list_entries(states=None)[0]], [item_id]
        )

    def test_read_commands_never_dispatch(self):
        self.cli("--json", "status")
        self.cli("--json", "usage")
        self.cli("--json", "doctor", "--chat", CHAT_ID)
        self.cli("--json", "chats", "list")
        self.cli("queue", "add", CHAT_ID)
        self.cli("--json", "queue", "check", "--limit", "1")
        self.assertEqual(self.store.get_queue_summary().dispatching, 0)

    def test_interrupt_exits_130_without_a_traceback(self):
        reading, release = threading.Event(), threading.Event()
        read_usage = self.service.read_usage

        def slow_read():
            reading.set()
            release.wait(5)
            return read_usage()

        with patch.object(self.service, "read_usage", side_effect=slow_read):
            process = subprocess.Popen(
                [sys.executable, "-m", "carrie_on", "--server", self.url, "usage"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertTrue(reading.wait(5))
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130, stderr)
                self.assertEqual(stdout, "")
                self.assertNotIn("Traceback", stderr)
            finally:
                release.set()
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_explicit_url_ignores_broken_settings_and_external_urls_fail(self):
        settings = self.home / "Library/Application Support/Carrie On/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("invalid")
        self.cli("--json", "queue", "list")
        self.cli("--server", "https://example.com", "queue", "list", code=2)
        self.cli("--server", "http://127.0.0.1:invalid", "queue", "list", code=2)


class ServiceProfiles(unittest.TestCase):
    def test_reinstall_preserves_selected_profiles_connection_settings(self):
        import plistlib

        from typer.testing import CliRunner

        from carrie_on.cli import app

        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "Library/Application Support/Carrie On Dev"
            data.mkdir(parents=True)
            saved = {
                "port": 8877,
                "codex": "/bin/echo",
                "interval": 90,
                "socket": "/fixture/socket",
                "codex_home": "/fixture/home",
            }
            (data / "settings.json").write_text(json.dumps(saved))
            stopped = subprocess.CompletedProcess([], 1, "", "")
            with (
                patch("carrie_on.macos.installation.subprocess.run", return_value=stopped),
                patch("carrie_on.macos.installation.verify_codex"),
            ):
                result = CliRunner().invoke(
                    app,
                    ["--profile", "dev", "service", "install"],
                    env={"HOME": temporary},
                )
            self.assertEqual(result.exit_code, 0, result.output)
            config = plistlib.loads(
                (data / "Lifecycle/Agents/com.nickcorin.carrieon.dev.ui.plist").read_bytes()
            )
            self.assertEqual(
                config["ProgramArguments"][-7:],
                [
                    "--port",
                    "8877",
                    "--interval",
                    "90",
                    "--socket",
                    "/fixture/socket",
                    "serve",
                ],
            )
            self.assertEqual(config["EnvironmentVariables"]["CODEX_HOME"], "/fixture/home")
            self.assertEqual(
                json.loads((data / "settings.json").read_text()),
                dict(saved, schema_version=1),
            )

    def test_dev_service_controls_never_target_release(self):
        result = subprocess.CompletedProcess([], 0, "running", "")
        with patch("carrie_on.macos.lifecycle.subprocess.run", return_value=result) as run:
            self.assertTrue(control_service("status", PROFILES["dev"])["running"])
        self.assertEqual(
            run.call_args.args[0][-1],
            f"gui/{os.getuid()}/com.nickcorin.carrieon.dev.ui",
        )

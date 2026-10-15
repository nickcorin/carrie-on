"""Reproduce source-runtime and installation recovery through public operations."""

import fcntl
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from carrie_on.macos.installation import (
    Settings,
    SettingsOverrides,
    SetupError,
    import_source_queue,
    install_service,
)
from carrie_on.macos.runtime import PROFILES, Runtime
from carrie_on.ports import StorageUnavailable
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class SourceRuntimeTests(unittest.TestCase):
    def test_virtual_environment_commands_run_outside_the_checkout(self):
        runtime = Runtime.current()
        self.assertEqual(runtime.executable, Path(sys.executable).absolute())
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as directory:
            client = subprocess.run(
                runtime.command("cli") + ["--version"],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            server = subprocess.run(
                runtime.command("server") + ["--help"],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
        self.assertEqual(client.stdout, f"carrie-on {runtime.version}\n")
        self.assertIn("--state", server.stdout)

    def test_runtime_discovery_retains_symlinks_and_selects_profiles_per_call(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            interpreter = home / "venv/bin/python"
            interpreter.parent.mkdir(parents=True)
            interpreter.symlink_to(sys.executable)
            with (
                patch("sys.executable", str(interpreter)),
                patch("pathlib.Path.home", return_value=home),
            ):
                release = Runtime.current(PROFILES["release"], registered=True)
                dev = Runtime.current(PROFILES["dev"], registered=True)
                source = Runtime.current()
            self.assertEqual(release.executable, interpreter)
            self.assertEqual(dev.data, home / "Library/Application Support/Carrie On Dev")
            self.assertEqual(release.data, home / "Library/Application Support/Carrie On")
            self.assertEqual(source.data, Path(__file__).resolve().parents[1])


class ServiceInstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.source = self.home / "checkout"
        self.source.mkdir()
        self.runtime = Runtime(self.source, self.home / "data", Path(sys.executable), False)
        self.runtime.data.mkdir()
        self.settings = self.runtime.data / "settings.json"
        home_patch = patch("pathlib.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.codex = self.home / "codex"
        self.codex.write_text(
            '#!/bin/sh\nprintf \'%s\' "$CODEX_HOME" > "' + str(self.home / "child-home") + '"\n'
        )
        self.codex.chmod(0o755)

    @property
    def agent(self):
        return self.home / "Library/LaunchAgents/com.nickcorin.carrieon.ui.plist"

    def test_direct_server_does_not_inherit_registered_legacy_settings(self):
        self.agent.parent.mkdir(parents=True)
        self.agent.write_bytes(
            plistlib.dumps(
                {
                    "ProgramArguments": [
                        "old-server",
                        "--codex",
                        "/registered/codex",
                        "--socket",
                        "/registered/socket",
                        "--port",
                        "9988",
                    ],
                    "StartInterval": 120,
                }
            )
        )
        isolated = Settings.load(self.runtime, SettingsOverrides(codex=str(self.codex)))
        self.assertEqual((isolated.port, isolated.interval, isolated.socket), (8766, 60, None))
        adopted = Settings.load(self.runtime, SettingsOverrides(codex=str(self.codex)), legacy=True)
        self.assertEqual(
            (adopted.port, adopted.interval, adopted.socket), (9988, 120, "/registered/socket")
        )

    def test_saved_configuration_is_validated_before_publication(self):
        self.agent.parent.mkdir(parents=True)
        self.agent.write_bytes(b"original service")
        for invalid in ({"interval": 0}, {"port": True}, {"socket": ""}):
            self.settings.write_text(json.dumps({"codex": str(self.codex), **invalid}))
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                install_service(self.runtime, start=False)
            self.assertEqual(self.agent.read_bytes(), b"original service")
        install_service(self.runtime, SettingsOverrides(socket=Path("/valid")), start=False)
        configuration = plistlib.loads(self.agent.read_bytes())
        self.assertIn("/valid", configuration["ProgramArguments"])

    def test_relative_socket_is_saved_once_and_parent_environment_is_unchanged(self):
        first, second = self.home / "first", self.home / "second"
        first.mkdir()
        second.mkdir()
        with patch.dict(os.environ, {"CODEX_HOME": "parent-choice"}):
            with working_directory(first):
                install_service(
                    self.runtime,
                    SettingsOverrides(
                        codex=str(self.codex),
                        socket=Path("ipc.sock"),
                        codex_home=self.home / "custom codex",
                    ),
                    start=False,
                )
            with working_directory(second):
                install_service(self.runtime, start=False)
            self.assertEqual(os.environ["CODEX_HOME"], "parent-choice")
        saved = json.loads(self.settings.read_text())
        self.assertEqual(saved["socket"], str(first / "ipc.sock"))
        self.assertEqual((self.home / "child-home").read_text(), str(self.home / "custom codex"))
        configuration = plistlib.loads(self.agent.read_bytes())
        self.assertEqual(
            configuration["ProgramArguments"][-3:],
            ["--socket", str(first / "ipc.sock"), "serve"],
        )
        self.assertEqual(
            configuration["EnvironmentVariables"]["CODEX_HOME"],
            str(self.home / "custom codex"),
        )

    def test_install_lock_covers_settings_read_and_service_publication(self):
        self.settings.write_text(json.dumps({"codex": str(self.codex), "port": 8877}))
        with (self.runtime.data / "install.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                install_service(self.runtime, SettingsOverrides(port=9999), start=False)
            self.assertEqual(json.loads(self.settings.read_text())["port"], 8877)
        result = install_service(self.runtime, start=False)
        self.assertEqual(result["port"], 8877)
        self.assertEqual(
            plistlib.loads(self.agent.read_bytes())["WorkingDirectory"],
            str(self.runtime.data),
        )

    def test_failed_publication_keeps_settings_for_idempotent_repair(self):
        with patch(
            "carrie_on.macos.installation.write_agent",
            side_effect=OSError("disk failure"),
        ):
            with self.assertRaisesRegex(OSError, "disk failure"):
                install_service(
                    self.runtime,
                    SettingsOverrides(codex=str(self.codex), port=8877),
                    start=False,
                )
        self.assertEqual(json.loads(self.settings.read_text())["port"], 8877)
        install_service(self.runtime, start=False)
        self.assertEqual(
            plistlib.loads(self.agent.read_bytes())["ProgramArguments"][-5:],
            ["--port", "8877", "--interval", "60", "serve"],
        )

    def test_database_failure_reports_recoverable_setup_error_and_retry_repairs(self):
        source = self.source / "resume-queue.json"
        source.write_text('{"version": 2, "entries": [], "history": []}')
        stopped = subprocess.CompletedProcess([], 1)
        with patch("carrie_on.macos.installation.subprocess.run", return_value=stopped):
            with patch.object(
                SqliteStorageMaintenance,
                "initialize",
                side_effect=StorageUnavailable("database is locked"),
            ):
                with self.assertRaises(SetupError) as caught:
                    install_service(self.runtime, SettingsOverrides(codex=str(self.codex)))
            self.assertEqual(caught.exception.code, "storage_unavailable")
            self.assertIsInstance(caught.exception.__cause__, StorageUnavailable)
            self.assertIn("retry setup", str(caught.exception))
            result = install_service(self.runtime, SettingsOverrides(codex=str(self.codex)))
        self.assertEqual(result["port"], 8766)
        self.assertEqual(
            plistlib.loads(self.agent.read_bytes())["WorkingDirectory"], str(self.runtime.data)
        )
        self.assertEqual(
            (self.runtime.data / "Migration/source-checkout/resume-queue.json").read_bytes(),
            source.read_bytes(),
        )

    def test_source_json_import_preserves_snapshot_and_does_not_reactivate(self):
        item_id = "80e3249d-00f4-44d2-99be-f07af239f686"
        payload = {
            "version": 2,
            "paused": True,
            "entries": [
                {
                    "id": item_id,
                    "thread_id": str(uuid4()),
                    "title": "Old task",
                    "account_id": "account",
                    "limit_id": "codex",
                    "anchor_message_id": "anchor",
                    "model": "model",
                    "prompt": "Keep working",
                    "reset_windows": {"primary": 123},
                    "enrolled_while_busy": False,
                }
            ],
            "history": [{"id": "old-history", "outcome": "resumed"}],
        }
        current_id = "22f01dc9-cd81-447a-8179-13c2bd6802c4"
        current = dict(
            payload["entries"][0],
            id=current_id,
            thread_id=str(uuid4()),
            prompt="Installed task",
        )
        (self.runtime.data / "resume-queue.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "entries": [current],
                    "history": [],
                }
            )
        )
        source = self.source / "resume-queue.json"
        source.write_text(json.dumps(payload))
        original = source.read_bytes()
        import_source_queue(replace(self.runtime, profile=PROFILES["dev"]))
        self.assertFalse(self.runtime.state.exists())
        run_process = subprocess.run

        def isolated_process(command, **kwargs):
            if command[0] == "/bin/launchctl":
                return subprocess.CompletedProcess(command, 1 if command[1] == "print" else 0)
            return run_process(command, **kwargs)

        with patch("carrie_on.macos.installation.subprocess.run", side_effect=isolated_process):
            install_service(self.runtime, SettingsOverrides(codex=str(self.codex)))
        database = SqliteQueueStorage(self.runtime.state)
        self.assertEqual(database.get_entry(UUID(item_id)).instructions, "Keep working")
        self.assertEqual(database.get_entry(UUID(current_id)).instructions, "Installed task")
        self.assertEqual(database.get_queue_summary().count, 2)
        database.update_entry_state(UUID(item_id), "cancelled")
        with patch("carrie_on.macos.installation.subprocess.run", side_effect=isolated_process):
            install_service(self.runtime)
        self.assertEqual(database.get_entry(UUID(item_id)).state, "cancelled")
        self.assertEqual(source.read_bytes(), original)
        snapshot = self.runtime.data / "Migration/source-checkout/resume-queue.json"
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertEqual(len(database.list_entries(states=None)[0]), 2)

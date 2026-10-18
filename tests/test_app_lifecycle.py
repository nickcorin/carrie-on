"""Login preferences survive service installation, upgrades, and removal."""

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from carrie_on.macos import lifecycle as app_lifecycle
from carrie_on.macos.runtime import PROFILES

PROFILE = PROFILES["release"]


class LifecycleRegistration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        mock = patch("pathlib.Path.home", return_value=self.home)
        mock.start()
        self.addCleanup(mock.stop)

    def preference(self, enabled):
        root = app_lifecycle.directory(PROFILE)
        root.mkdir(parents=True, exist_ok=True)
        (root / "preferences.plist").write_bytes(plistlib.dumps({"launch_at_login": enabled}))

    def test_enabled_registration_keeps_manual_launch_copy(self):
        config = {"Label": PROFILE.labels[0], "ProgramArguments": ["/worker", "tick"]}
        path = app_lifecycle.write_agent(config, PROFILE)
        self.assertEqual(path.parent, self.home / "Library/LaunchAgents")
        self.assertEqual(plistlib.loads(path.read_bytes()), config)
        saved = app_lifecycle.directory(PROFILE) / "Agents" / path.name
        self.assertEqual(plistlib.loads(saved.read_bytes()), config)

    def test_unregistration_waits_for_unload_and_tolerates_absent_jobs(self):
        loaded = subprocess.CompletedProcess([], 0)
        absent = subprocess.CompletedProcess([], 1)
        with (
            patch.object(
                app_lifecycle.subprocess, "run", side_effect=[loaded, loaded, loaded, absent]
            ) as run,
            patch.object(app_lifecycle.time, "sleep") as sleep,
        ):
            app_lifecycle.unregister_services((PROFILE.labels[0],))
        self.assertEqual(
            [call.args[0][1] for call in run.call_args_list], ["print", "bootout", "print", "print"]
        )
        sleep.assert_called_once_with(0.1)
        with patch.object(app_lifecycle.subprocess, "run", return_value=absent) as run:
            app_lifecycle.unregister_services((PROFILE.labels[0],))
        self.assertEqual(run.call_count, 1)

    def test_failed_or_unfinished_unregistration_is_reported(self):
        loaded = subprocess.CompletedProcess([], 0)
        with patch.object(
            app_lifecycle.subprocess,
            "run",
            side_effect=[loaded, subprocess.CalledProcessError(1, "bootout")],
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                app_lifecycle.unregister_services((PROFILE.labels[0],))
        with (
            patch.object(app_lifecycle.subprocess, "run", return_value=loaded),
            patch.object(app_lifecycle.time, "sleep"),
        ):
            with self.assertRaisesRegex(ValueError, "has not unloaded"):
                app_lifecycle.unregister_services((PROFILE.labels[0],))

    def test_start_kickstarts_a_loaded_service_without_killing_it(self):
        stopped = subprocess.CompletedProcess([], 0, "state = waiting", "")
        with patch.object(app_lifecycle.subprocess, "run", return_value=stopped) as run:
            app_lifecycle.control_service("start", PROFILE)
        self.assertEqual(
            run.call_args.args[0][1:],
            ["kickstart", f"gui/{app_lifecycle.os.getuid()}/{PROFILE.labels[0]}"],
        )

    def test_disabled_startup_survives_reinstall_and_updates_manual_definitions(self):
        for label in PROFILE.labels:
            app_lifecycle.write_agent({"Label": label, "ProgramArguments": ["/old"]}, PROFILE)
        self.preference(False)
        for label in PROFILE.labels:
            config = {"Label": label, "ProgramArguments": ["/new"]}
            path = app_lifecycle.write_agent(config, PROFILE)
            self.assertEqual(path, app_lifecycle.directory(PROFILE) / "Agents" / f"{label}.plist")
            self.assertEqual(plistlib.loads(path.read_bytes()), config)
            self.assertFalse((self.home / "Library/LaunchAgents" / path.name).exists())
        self.assertFalse(app_lifecycle.launch_at_login(PROFILE))

    def test_uninstall_removes_manual_definition_too(self):
        label = PROFILE.labels[0]
        path = app_lifecycle.write_agent({"Label": label, "ProgramArguments": ["/worker"]}, PROFILE)
        app_lifecycle.remove_agent(label, PROFILE)
        app_lifecycle.remove_agent(label, PROFILE)
        self.assertFalse(path.exists())
        self.assertFalse((app_lifecycle.directory(PROFILE) / "Agents" / path.name).exists())
        self.assertTrue(app_lifecycle.launch_at_login(PROFILE))

    def test_invalid_preference_does_not_overwrite_service(self):
        label = PROFILE.labels[0]
        config = {"Label": label, "ProgramArguments": ["/worker"]}
        path = app_lifecycle.write_agent(config, PROFILE)
        self.preference("false")
        with self.assertRaisesRegex(ValueError, "Invalid launch-at-login"):
            app_lifecycle.write_agent({"Label": label, "ProgramArguments": ["/changed"]}, PROFILE)
        self.assertEqual(plistlib.loads(path.read_bytes()), config)

"""Login preferences survive service installation, upgrades, and removal."""

import plistlib
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

"""Login preferences survive service installation, upgrades, and removal."""

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_lifecycle


class LifecycleRegistration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        mock = patch("pathlib.Path.home", return_value=self.home)
        mock.start()
        self.addCleanup(mock.stop)

    def preference(self, enabled):
        root = app_lifecycle.directory()
        root.mkdir(parents=True, exist_ok=True)
        (root / "preferences.plist").write_bytes(plistlib.dumps({"launch_at_login": enabled}))

    def test_enabled_registration_keeps_manual_launch_copy(self):
        config = {"Label": app_lifecycle.LABELS[0], "ProgramArguments": ["/worker", "tick"]}
        path = app_lifecycle.write_agent(config)
        self.assertEqual(path.parent, self.home / "Library/LaunchAgents")
        self.assertEqual(plistlib.loads(path.read_bytes()), config)
        saved = app_lifecycle.directory() / "Agents" / path.name
        self.assertEqual(plistlib.loads(saved.read_bytes()), config)

    def test_disabled_startup_survives_reinstall_and_updates_manual_definitions(self):
        for label in app_lifecycle.LABELS:
            app_lifecycle.write_agent({"Label": label, "ProgramArguments": ["/old"]})
        self.preference(False)
        for label in app_lifecycle.LABELS:
            config = {"Label": label, "ProgramArguments": ["/new"]}
            path = app_lifecycle.write_agent(config)
            self.assertEqual(path, app_lifecycle.directory() / "Agents" / f"{label}.plist")
            self.assertEqual(plistlib.loads(path.read_bytes()), config)
            self.assertFalse((self.home / "Library/LaunchAgents" / path.name).exists())
        self.assertFalse(app_lifecycle.launch_at_login())

    def test_uninstall_removes_manual_definition_too(self):
        label = app_lifecycle.LABELS[0]
        path = app_lifecycle.write_agent({"Label": label, "ProgramArguments": ["/worker"]})
        app_lifecycle.remove_agent(label)
        app_lifecycle.remove_agent(label)
        self.assertFalse(path.exists())
        self.assertFalse((app_lifecycle.directory() / "Agents" / path.name).exists())
        self.assertTrue(app_lifecycle.launch_at_login())

    def test_invalid_preference_does_not_overwrite_service(self):
        label = app_lifecycle.LABELS[0]
        config = {"Label": label, "ProgramArguments": ["/worker"]}
        path = app_lifecycle.write_agent(config)
        self.preference("false")
        with self.assertRaisesRegex(ValueError, "Invalid launch-at-login"):
            app_lifecycle.write_agent({"Label": label, "ProgramArguments": ["/changed"]})
        self.assertEqual(plistlib.loads(path.read_bytes()), config)

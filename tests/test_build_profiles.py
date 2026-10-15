"""Installed profiles share behavior without sharing queue or service state."""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from carrie_on.macos.runtime import read_profile

ROOT = Path(__file__).resolve().parents[1]


class BuildProfiles(unittest.TestCase):
    def test_profiles_keep_independent_queues_and_login_preferences(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir()
            for profile in ("release", "dev"):
                resources = root / profile
                resources.mkdir()
                shutil.copy2(ROOT / "VERSION", resources / "VERSION")
                (resources / "build-profile.json").write_text(json.dumps({"profile": profile}))
                script = """
import sys
sys.frozen = True
sys._MEIPASS = sys.argv[1]
import argparse, json, plistlib
from carrie_on.macos import runtime as app_runtime, lifecycle as app_lifecycle, installation as bundle_install
from carrie_on.storage import QueueStore
runtime = app_runtime.Runtime.current()
runtime.data.mkdir(parents=True)
QueueStore(runtime.state).initialize()
settings = bundle_install.Settings.load(runtime, bundle_install.SettingsOverrides(codex="/bin/echo"))
(runtime.data / "settings.json").write_text(json.dumps({"port": settings.port}))
bundle_install.install_service(runtime, bundle_install.SettingsOverrides(codex="/bin/echo"), start=False)
if runtime.profile.name == "dev":
    (app_lifecycle.directory(runtime.profile) / "preferences.plist").write_bytes(
        plistlib.dumps({"launch_at_login": False}))
    bundle_install.install_service(runtime, bundle_install.SettingsOverrides(codex="/bin/echo"), start=False)
"""
                subprocess.run(
                    [sys.executable, "-c", script, str(resources)],
                    cwd=resources,
                    env=dict(os.environ, HOME=str(home)),
                    check=True,
                    capture_output=True,
                )
            release = home / "Library/Application Support/Carrie On"
            dev = home / "Library/Application Support/Carrie On Dev"
            for data, port in ((release, 8766), (dev, 8767)):
                self.assertTrue((data / "queue.sqlite3").is_file())
                self.assertFalse((data / "resume-queue.json").exists())
                self.assertEqual(json.loads((data / "settings.json").read_text())["port"], port)
            agents = home / "Library/LaunchAgents"
            worker = plistlib.loads((agents / "com.nickcorin.carrieon.ui.plist").read_bytes())
            self.assertEqual(worker["WorkingDirectory"], str(release))
            dev_worker = plistlib.loads(
                (dev / "Lifecycle/Agents/com.nickcorin.carrieon.dev.ui.plist").read_bytes()
            )
            self.assertEqual(dev_worker["WorkingDirectory"], str(dev))
            self.assertTrue(dev_worker["KeepAlive"] and worker["KeepAlive"])
            self.assertFalse((agents / "com.nickcorin.carrieon.dev.ui.plist").exists())

    def test_unknown_packaged_profile_fails_before_using_release_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary)
            (resources / "build-profile.json").write_text('{"profile":"unknown"}')
            with self.assertRaisesRegex(ValueError, "Unknown build profile"):
                read_profile(resources, True)
            self.assertEqual(read_profile(resources, False).name, "release")

    def test_dev_updater_does_not_fetch_production(self):
        from carrie_on.macos import updates as app_update
        from carrie_on.macos.runtime import PROFILES, Runtime

        with self.assertRaisesRegex(ValueError, "rebuilding locally"):
            app_update.check_update(Runtime.current(PROFILES["dev"]))

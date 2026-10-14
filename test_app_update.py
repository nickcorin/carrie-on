"""Exercise release selection, hostile downloads, staging, and native replacement."""

import hashlib
import json
import platform
import plistlib
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import app_update


ROOT = Path(__file__).resolve().parent


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.version = "0.10.0"
        self.name = "Carrie-On-0.10.0-macos-arm64"
        self.release = {"tag_name": "v0.10.0", "draft": False, "prerelease": False,
                        "assets": [{"name": self.name + suffix, "state": "uploaded",
                                    "browser_download_url": f"https://github.com/nickcorin/carrie-on/releases/download/v0.10.0/{self.name}{suffix}"}
                                   for suffix in (".zip", ".json")]}
        self.manifest = {"version": "0.10.0", "architecture": "arm64", "archive": self.name + ".zip",
                         "sha256": "a" * 64, "bytes": 1024, "minimum_macos": "13.0"}

    def read(self, current="0.9.0", architecture="arm64", macos="13.0"):
        return app_update.UpdateRelease.read(self.release, self.manifest, current, architecture, macos)

    def test_numeric_versions_and_no_downgrades(self):
        self.assertEqual(self.read().version, "0.10.0")
        self.assertIsNone(self.read(current="0.10.0"))
        self.assertIsNone(self.read(current="1.0.0"))

    def test_ineligible_and_incomplete_releases(self):
        for key in ("draft", "prerelease"):
            with self.subTest(key=key), patch.dict(self.release, {key: True}):
                with self.assertRaisesRegex(ValueError, "stable"):
                    self.read()
        with self.assertRaisesRegex(ValueError, "this Mac"):
            self.read(architecture="x86_64")
        with self.assertRaisesRegex(ValueError, "requires macOS"):
            self.read(macos="12.7")
        with patch.dict(self.release, {"assets": []}):
            with self.assertRaisesRegex(ValueError, "missing"):
                self.read()
        with patch.dict(self.manifest, {"bytes": True}):
            with self.assertRaisesRegex(ValueError, "size"):
                self.read()
        with patch.dict(self.manifest, {"sha256": "garbage"}):
            with self.assertRaisesRegex(ValueError, "checksum"):
                self.read()

    def archive(self, entries):
        archive = self.root / "update.zip"
        with zipfile.ZipFile(archive, "w") as package:
            for name, value in entries.items():
                if isinstance(value, tuple):
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    package.writestr(info, value[0])
                else:
                    package.writestr(name, value)
        release = replace(self.read(), sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), size=archive.stat().st_size)
        return archive, release

    def test_verified_archive_and_corrupt_download(self):
        archive, release = self.archive({"Carrie On.app/Contents/Resources/VERSION": "0.10.0",
                                         "Carrie On.app/Contents/Frameworks/VERSION": ("../Resources/VERSION",)})
        app_update.validate_archive(archive, release)
        with self.assertRaisesRegex(ValueError, "checksum"):
            app_update.validate_archive(archive, replace(release, sha256="b" * 64))
        archive.write_bytes(b"truncated")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            app_update.validate_archive(archive, release)

    def test_hostile_archive_paths_and_links(self):
        cases = [
            {"../outside": "bad"},
            {"/tmp/outside": "bad"},
            {"Carrie On.app/escape": ("../../outside",)},
            {"Carrie On.app/link": ("/tmp",)},
            {"Carrie On.app/link": ("target",), "Carrie On.app/link/child": "bad"},
            {"Carrie On.app/link": ("target",), "Carrie On.app/LINK/child": "bad"},
            {"Carrie On.app/a": ("b",), "Carrie On.app/b": ("a",)},
            {"Carrie On.app/a": (".",), "Carrie On.app/b": ("a/../outside",)},
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                archive, release = self.archive(entries)
                with self.assertRaises(ValueError):
                    app_update.validate_archive(archive, release)

    def test_check_pins_the_accepted_version(self):
        with patch.object(app_update, "VERSION", "0.9.0"), patch.object(app_update.platform, "machine", return_value="arm64"), patch.object(app_update.platform, "mac_ver", return_value=("13.0", (), "")):
            with patch.object(app_update, "read_json", side_effect=[self.release, self.manifest]) as read:
                self.assertEqual(app_update.check_update("0.10.0").version, "0.10.0")
                self.assertEqual(read.call_args_list[0].args, ("https://api.github.com/repos/nickcorin/carrie-on/releases/tags/v0.10.0",))
            with patch.object(app_update, "read_json", return_value=self.release):
                with self.assertRaisesRegex(ValueError, "changed"):
                    app_update.check_update("0.11.0")

    def test_no_public_release_and_withdrawn_update(self):
        response = subprocess.CompletedProcess([], 22, stdout="404", stderr="not found")
        with patch.object(app_update.subprocess, "run", return_value=response):
            self.assertIsNone(app_update.check_update())
            with self.assertRaisesRegex(app_update.ReleaseUnavailable, "not publicly available"):
                app_update.check_update("0.10.0")
        response = subprocess.CompletedProcess([], 22, stdout="403", stderr="rate limited")
        with patch.object(app_update.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(ValueError, "Wait a few minutes"):
                app_update.check_update()

    def test_failed_download_leaves_installed_app_and_no_staging(self):
        app = self.root / "Carrie On.app"
        app.mkdir()
        (app / "original").write_text("keep me")
        with patch.object(app_update, "download", side_effect=ValueError("offline")):
            with self.assertRaisesRegex(ValueError, "offline"):
                app_update.prepare_update(self.read(), app)
        self.assertEqual((app / "original").read_text(), "keep me")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["Carrie On.app"])

    @unittest.skipUnless(platform.system() == "Darwin", "App signing requires macOS")
    def test_staging_real_signed_bundle_and_rejecting_wrong_identity(self):
        app = self.root / "Carrie On.app"
        contents = app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Resources").mkdir()
        shutil.copyfile("/usr/bin/true", contents / "MacOS/CarrieOnMenu")
        (contents / "MacOS/CarrieOnMenu").chmod(0o755)
        info = {"CFBundleIdentifier": "com.nickcorin.carrieon.app", "CFBundleShortVersionString": "0.10.0",
                "CFBundleExecutable": "CarrieOnMenu", "CarrieOnStandalone": True}
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        (contents / "Resources/release.json").write_text(json.dumps({"version": "0.10.0", "architecture": platform.machine(), "minimum_macos": "13.0"}))
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True, capture_output=True)
        archive = self.root / "signed.zip"
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
        release = replace(self.read(), sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), size=archive.stat().st_size)
        with patch.object(app_update, "download", side_effect=lambda url, dest, limit: shutil.copy2(archive, dest)):
            candidate = app_update.prepare_update(release, app)
            self.assertEqual(plistlib.loads((candidate / "Contents/Info.plist").read_bytes())["CFBundleShortVersionString"], "0.10.0")
            self.assertEqual((contents / "Info.plist").read_bytes(), (candidate / "Contents/Info.plist").read_bytes())
            with self.assertRaisesRegex(ValueError, "does not match"):
                app_update.prepare_update(replace(release, version="0.11.0"), app)


@unittest.skipUnless(platform.system() == "Darwin", "Atomic app replacement requires macOS")
class NativeUpdateTests(unittest.TestCase):
    def test_install_rollback_and_retry(self):
        with tempfile.TemporaryDirectory(prefix="carrie-update-test-") as temporary:
            root = Path(temporary)
            main = root / "main.swift"
            shutil.copyfile(ROOT / "tests/UpdateInstallation.swift", main)
            executable = root / "UpdateTests"
            result = subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(root / "cache"), "-o", str(executable),
                                     str(ROOT / "menubar/AppUpdate.swift"), str(main)], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Update installation checks passed", result.stdout)
            previous = subprocess.Popen(["/bin/sleep", "10"])
            try:
                replacement = subprocess.Popen([str(executable), "--relaunch-after", str(previous.pid)],
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                time.sleep(0.3)
                self.assertIsNone(replacement.poll(), "Replacement started before the old instance exited")
                previous.terminate()
                previous.wait(timeout=5)
                output, errors = replacement.communicate(timeout=5)
                self.assertEqual(replacement.returncode, 0, errors)
                self.assertEqual(output.strip(), "Previous instance exited")
            finally:
                if previous.poll() is None:
                    previous.terminate()
                    previous.wait(timeout=5)

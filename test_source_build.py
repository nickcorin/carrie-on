"""Source builds stage replacements without disrupting the installed app."""

import plistlib
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install_menubar
import build_bundle
from app_runtime import PROFILES

ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(platform.system() == "Darwin", "Native menu builds require macOS")
class SourceBuildTests(unittest.TestCase):
    def test_bundle_staging_rejects_release_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "Dev.app"
            with self.assertRaisesRegex(ValueError, "Only dev builds"):
                build_bundle.build(root, prepare_app=candidate)
            candidate.mkdir()
            (candidate / "version").write_text("working")
            with self.assertRaisesRegex(ValueError, "already exists"):
                build_bundle.build(root, profile=PROFILES["dev"], prepare_app=candidate)
            self.assertEqual((candidate / "version").read_text(), "working")

    def test_native_build_output_failure_and_successful_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "source-build-checks"
            subprocess.run([
                "/usr/bin/swiftc", "-parse-as-library", "-module-cache-path", str(root / "cache"),
                "-o", str(executable), str(ROOT / "menubar/AppUpdate.swift"),
                str(ROOT / "tests/SourceBuild.swift"),
            ], check=True, capture_output=True, text=True)
            result = subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.stdout, "Source rebuild checks passed\n")

    def test_prepare_refuses_an_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / "Installed.app"
            installed.mkdir()
            (installed / "version").write_text("working")
            with self.assertRaises(FileExistsError):
                install_menubar.prepare_app(installed, 8877)
            self.assertEqual((installed / "version").read_text(), "working")

    def test_prepared_bundle_retains_checkout_interpreter_and_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "menu"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            candidate = root / "replacement/Menu.app"
            with patch.object(install_menubar, "build_menu", return_value=executable):
                install_menubar.prepare_app(candidate, 8877)
            info = plistlib.loads((candidate / "Contents/Info.plist").read_bytes())
            self.assertEqual(info["CarrieOnPort"], "8877")
            self.assertEqual(info["CarrieOnSourceRoot"], str(ROOT))
            self.assertEqual(info["CarrieOnBuildPython"], str(install_menubar.RUNTIME.executable))
            self.assertEqual((candidate / "Contents/MacOS/CarrieOnMenu").read_text(), "#!/bin/sh\nexit 0\n")
            subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(candidate)], check=True)


if __name__ == "__main__":
    unittest.main()

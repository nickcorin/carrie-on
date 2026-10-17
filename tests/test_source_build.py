"""Source builds stage replacements without disrupting the installed app."""

import platform
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from carrie_on.macos.runtime import PROFILES, Runtime
from tools import build_bundle, install_menubar

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(platform.system() == "Darwin", "Native menu builds require macOS")
class SourceBuildTests(unittest.TestCase):
    def test_local_commit_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "checkout"
            repo.mkdir()

            def git(*args, input=None):
                return subprocess.check_output(
                    ["git", *args],
                    cwd=repo,
                    input=input,
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()

            git("init", "-b", "master")
            tree = git("hash-object", "-w", "-t", "tree", "--stdin", input="")

            def commit(message, parent=None):
                data = f"tree {tree}\n" + (f"parent {parent}\n" if parent else "")
                data += f"author Test <test@example.invalid> 1 +0000\ncommitter Test <test@example.invalid> 1 +0000\n\n{message}\n"
                return git("hash-object", "-w", "-t", "commit", "--stdin", input=data)

            base = commit("base")
            newer = commit("newer", base)
            other = commit("unrelated")
            contents = root / "Dev.app/Contents"
            contents.mkdir(parents=True)
            metadata = {
                "CFBundleIdentifier": "test.commit-discovery",
                "CFBundlePackageType": "APPL",
                "CarrieOnSourceRoot": str(repo),
                "CarrieOnBuildPython": "/bin/sh",
                "CarrieOnPort": "8877",
                "CarrieOnSourceCommit": base,
            }
            (contents / "Info.plist").write_bytes(plistlib.dumps(metadata))
            main = root / "main.swift"
            main.write_text(
                'import Foundation\nlet build = try SourceBuild(bundle: Bundle(path: CommandLine.arguments[1])!)\nprint(try build.checkoutCommit() ?? "none")\n'
            )
            executable = root / "commit-check"
            subprocess.run(
                [
                    "/usr/bin/swiftc",
                    "-module-cache-path",
                    str(root / "cache"),
                    "-o",
                    str(executable),
                    str(ROOT / "menubar/AppUpdate.swift"),
                    str(main),
                ],
                check=True,
                capture_output=True,
            )

            def discovered(head):
                git("update-ref", "refs/heads/master", head)
                return subprocess.check_output(
                    [str(executable), str(contents.parent)], text=True
                ).strip()

            self.assertEqual(discovered(base), "none")
            self.assertEqual(discovered(newer), newer)
            self.assertEqual(discovered(other), "none")
            metadata["CarrieOnSourceCommit"] = newer
            (contents / "Info.plist").write_bytes(plistlib.dumps(metadata))
            self.assertEqual(discovered(base), "none")
            del metadata["CarrieOnSourceCommit"]
            (contents / "Info.plist").write_bytes(plistlib.dumps(metadata))
            self.assertEqual(discovered(newer), "none")

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
            subprocess.run(
                [
                    "/usr/bin/swiftc",
                    "-parse-as-library",
                    "-module-cache-path",
                    str(root / "cache"),
                    "-o",
                    str(executable),
                    str(ROOT / "menubar/AppUpdate.swift"),
                    str(ROOT / "tests/SourceBuild.swift"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            result = subprocess.run(
                [str(executable)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
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
            self.assertEqual(info["CarrieOnBuildPython"], str(Runtime.current().executable))
            self.assertEqual(
                (candidate / "Contents/MacOS/CarrieOnMenu").read_text(),
                "#!/bin/sh\nexit 0\n",
            )
            subprocess.run(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(candidate)],
                check=True,
            )


if __name__ == "__main__":
    unittest.main()

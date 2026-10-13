import argparse
from contextlib import ExitStack
import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_runtime import Runtime
import bundle_install
import tokenmaxxer
from macos_compatibility import audit, deployment_versions, version_tuple


class DistributionTests(unittest.TestCase):
    def test_binary_audit_rejects_newer_os_and_external_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary)
            (payload / "runtime").write_bytes(bytes.fromhex("cffaedfe"))
            with patch("macos_compatibility.subprocess.check_output", return_value="cmd LC_BUILD_VERSION\nminos 26.0"):
                with self.assertRaisesRegex(ValueError, "requires macOS 26.0"):
                    audit(payload, "13.0")
            with patch("macos_compatibility.subprocess.check_output", side_effect=[
                "cmd LC_BUILD_VERSION\nminos 11.0", "runtime:\n /opt/homebrew/lib/example.dylib (compatibility version 1.0)"]):
                with self.assertRaisesRegex(ValueError, "external library"):
                    audit(payload, "13.0")
            with patch("macos_compatibility.subprocess.check_output", side_effect=[
                "cmd LC_BUILD_VERSION\nminos 11.0", "runtime:\n /usr/lib/libSystem.B.dylib (compatibility version 1.0)"]):
                self.assertEqual(audit(payload, "13.0"),
                                 {"binaries_checked": 1, "highest_deployment_target": "11.0"})

    def test_deployment_audit_reads_both_macos_load_command_formats(self):
        commands = """Load command 0
      cmd LC_BUILD_VERSION
    minos 13.0
      sdk 26.5
   ntools 1
     tool LD
  version 1267.0
Load command 1
      cmd LC_ID_DYLIB
  current version 3.14.0
Load command 2
      cmd LC_VERSION_MIN_MACOSX
  version 10.15
      sdk 14.0
"""
        self.assertEqual(deployment_versions(commands), ["13.0", "10.15"])
        self.assertEqual(version_tuple("13"), (13, 0, 0))
        self.assertEqual(version_tuple("13.0.1"), (13, 0, 1))

    def test_launch_commands_follow_runtime_not_build_machine(self):
        source = Runtime(Path("/source tree"), Path("/source tree"), Path("/usr/bin/python3"), False)
        installed = Runtime(Path("/app/_internal"), Path("/User Data"), Path("/app/tokenmaxxer"), True)
        self.assertEqual(source.command("worker"), ["/usr/bin/python3", "/source tree/tokenmaxxer.py"])
        self.assertEqual(source.command("dashboard"), ["/usr/bin/python3", "/source tree/tokenmaxxer_ui.py"])
        self.assertEqual(installed.command("worker"), ["/app/tokenmaxxer"])
        self.assertEqual(installed.command("dashboard"), ["/app/tokenmaxxer", "dashboard"])
        self.assertEqual(installed.state, Path("/User Data/resume-queue.json"))

    def test_worker_registration_uses_installed_runtime_and_private_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            data = home / "User Data"
            data.mkdir()
            runtime = Runtime(home / "app/_internal", data, home / "app/tokenmaxxer", True)
            args = argparse.Namespace(command="install", interval=90, codex="codex", socket=None,
                                      state=runtime.state, start=False)
            with patch("tokenmaxxer.RUNTIME", runtime), patch("pathlib.Path.home", return_value=home), \
                 patch("shutil.which", return_value="/cli/codex"), patch("sys.platform", "darwin"):
                tokenmaxxer.launch_agent(args)
            path = home / "Library/LaunchAgents/com.nickcorin.tokenmaxxer.plist"
            config = plistlib.loads(path.read_bytes())
            self.assertEqual(config["ProgramArguments"], [str(home / "app/tokenmaxxer"), "--state",
                             str(data / "resume-queue.json"), "--codex", "/cli/codex", "tick"])
            self.assertEqual(config["StartInterval"], 90)
            self.assertEqual(config["WorkingDirectory"], str(data))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)



class FirstLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.app = self.home / "Applications/Carrie On.app"
        contents = self.app / "Contents"
        (contents / "Resources").mkdir(parents=True)
        (contents / "MacOS").mkdir()
        (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_install.APP_IDENTIFIER}))
        self.metadata = {"architecture": "arm64", "minimum_macos": "13.0", "build_id": "first-build"}
        self.release = contents / "Resources/release.json"
        self.release.write_text(json.dumps(self.metadata))
        self.data = self.home / "Library/Application Support/Tokenmaxxer"
        self.data.mkdir(parents=True)
        self.runtime = Runtime(contents / "Resources", self.data, contents / "MacOS/tokenmaxxer", True)
        self.codex = self.home / "Codex CLI"
        self.codex.write_text("#!/bin/sh\nexit 0\n")
        self.codex.chmod(0o755)
        self.loaded = set()
        self.restarts = []
        self.fail_bootstrap = None
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        for target, value in (("bundle_install.RUNTIME", self.runtime), ("tokenmaxxer.RUNTIME", self.runtime),
                              ("tokenmaxxer_ui.RUNTIME", self.runtime)):
            contexts.enter_context(patch(target, value))
        contexts.enter_context(patch("pathlib.Path.home", return_value=self.home))
        contexts.enter_context(patch("platform.machine", return_value="arm64"))
        contexts.enter_context(patch("platform.mac_ver", return_value=("13.0", (), "")))
        contexts.enter_context(patch("sys.platform", "darwin"))
        contexts.enter_context(patch.dict(bundle_install.os.environ))
        contexts.enter_context(patch("bundle_install.subprocess.run", side_effect=self.run_process))
        contexts.enter_context(patch("bundle_install.wait_for_dashboard"))

    def run_process(self, command, **kwargs):
        from subprocess import CompletedProcess, CalledProcessError
        if command[0] == "/bin/launchctl":
            label = (Path(command[-1]).stem if command[1] == "bootstrap" else command[-1].split("/")[-1])
            if command[1] == "print":
                return CompletedProcess(command, 0 if label in self.loaded else 1)
            if command[1] == "bootout":
                self.loaded.discard(label)
                self.restarts.append(label)
            if command[1] == "bootstrap":
                if label == self.fail_bootstrap:
                    raise CalledProcessError(5, command)
                self.loaded.add(label)
        return CompletedProcess(command, 0)

    def args(self, **overrides):
        values = dict(codex=None, port=None, interval=None, socket=None, codex_home=None,
                      no_start=False, repair=False)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_first_launch_and_update_preserve_state_and_configuration(self):
        self.runtime.state.write_text('{"entries": ["private queue"], "history": ["old result"]}')
        original_queue = self.runtime.state.read_bytes()
        result = bundle_install.setup(self.args(codex=str(self.codex), port=8877, interval=90,
                                                 codex_home=str(self.home / "Custom Codex")))
        self.assertTrue(result["started"])
        self.assertEqual(self.loaded, set(bundle_install.LABELS))
        self.restarts.clear()
        bundle_install.setup(self.args())
        self.assertEqual(self.restarts, [], "Ordinary reopening must not restart services")
        self.metadata["build_id"] = "replacement-build"
        self.release.write_text(json.dumps(self.metadata))
        before_app = {path: path.read_bytes() for path in self.app.rglob("*") if path.is_file()}
        bundle_install.setup(self.args())
        self.assertEqual(set(self.restarts), set(bundle_install.LABELS[:2]))
        settings = json.loads((self.data / "settings.json").read_text())
        self.assertEqual((settings["codex"], settings["port"], settings["interval"]), (str(self.codex), 8877, 90))
        marker = json.loads((self.data / "installation.json").read_text())
        self.assertEqual(marker["build_id"], "replacement-build")
        self.assertEqual(self.runtime.state.read_bytes(), original_queue)
        self.assertEqual(before_app, {path: path.read_bytes() for path in self.app.rglob("*") if path.is_file()})
        worker = plistlib.loads((self.home / "Library/LaunchAgents/com.nickcorin.tokenmaxxer.plist").read_bytes())
        self.assertEqual(worker["ProgramArguments"][0], str(self.runtime.executable))
        self.assertEqual(worker["EnvironmentVariables"]["CODEX_HOME"], str(self.home / "Custom Codex"))

    def test_failed_registration_is_repaired_on_next_launch(self):
        from subprocess import CalledProcessError
        self.fail_bootstrap = bundle_install.tokenmaxxer_ui.LABEL
        with self.assertRaises(CalledProcessError):
            bundle_install.setup(self.args(codex=str(self.codex), port=8877))
        self.assertFalse((self.data / "installation.json").exists())
        self.fail_bootstrap = None
        bundle_install.setup(self.args())
        self.assertEqual(self.loaded, set(bundle_install.LABELS))
        self.assertEqual(json.loads((self.data / "installation.json").read_text())["settings"]["port"], 8877)

    def test_reopening_preserves_saved_default_despite_a_different_launch_environment(self):
        bundle_install.os.environ.pop("CODEX_HOME", None)
        bundle_install.setup(self.args(codex=str(self.codex)))
        settings_path = self.data / "settings.json"
        saved = settings_path.read_bytes()
        self.restarts.clear()
        bundle_install.os.environ["CODEX_HOME"] = str(self.home / "Other Codex")
        result = bundle_install.setup(self.args())
        self.assertTrue(result["started"])
        self.assertEqual(json.loads(settings_path.read_text())["codex_home"], None)
        self.assertEqual(settings_path.read_bytes(), saved)
        self.assertEqual(self.restarts, [])
        worker = plistlib.loads((self.home / "Library/LaunchAgents/com.nickcorin.tokenmaxxer.plist").read_bytes())
        self.assertNotIn("CODEX_HOME", worker["EnvironmentVariables"])

    def test_initial_environment_is_captured_and_explicit_setting_can_change_it(self):
        initial_home = str(self.home / "Initial Codex")
        bundle_install.os.environ["CODEX_HOME"] = initial_home
        bundle_install.setup(self.args(codex=str(self.codex)))
        settings_path = self.data / "settings.json"
        self.assertEqual(json.loads(settings_path.read_text())["codex_home"], initial_home)
        changed_home = str(self.home / "Chosen Codex")
        bundle_install.setup(self.args(codex_home=changed_home))
        self.assertEqual(json.loads(settings_path.read_text())["codex_home"], changed_home)

    def test_no_start_cannot_modify_existing_service_registration(self):
        bundle_install.setup(self.args(codex=str(self.codex), no_start=True))
        self.assertEqual(self.loaded, set())
        before = (self.data / "settings.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "fresh installation"):
            bundle_install.setup(self.args(port=9000, no_start=True))
        self.assertEqual((self.data / "settings.json").read_bytes(), before)

    def test_legacy_connection_settings_are_migrated(self):
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / f"{tokenmaxxer.LABEL}.plist").write_bytes(plistlib.dumps({
            "ProgramArguments": ["old-runtime", "--codex", str(self.codex), "--socket", "/custom/ipc.sock", "tick"],
            "StartInterval": 90, "EnvironmentVariables": {"CODEX_HOME": "/custom/codex"}}))
        (agents / f"{bundle_install.tokenmaxxer_ui.LABEL}.plist").write_bytes(plistlib.dumps({
            "ProgramArguments": ["old-runtime", "--codex", str(self.codex), "--port", "8877", "serve"]}))
        bundle_install.setup(self.args())
        settings = json.loads((self.data / "settings.json").read_text())
        self.assertEqual((settings["port"], settings["interval"], settings["socket"], settings["codex_home"]),
                         (8877, 90, "/custom/ipc.sock", "/custom/codex"))

    def test_wrong_bundle_and_translocated_app_are_rejected_before_install(self):
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "other.app"}))
        with self.assertRaisesRegex(ValueError, "inside Carrie On.app"):
            bundle_install.setup(self.args())
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_install.APP_IDENTIFIER}))
        moved = self.home / "Downloads/Carrie On.app"
        moved.parent.mkdir()
        self.app.rename(moved)
        runtime = Runtime(moved / "Contents/Resources", self.data, moved / "Contents/MacOS/tokenmaxxer", True)
        with patch("bundle_install.RUNTIME", runtime):
            with self.assertRaises(bundle_install.SetupError) as caught:
                bundle_install.setup(self.args())
        self.assertEqual(caught.exception.code, "move_app")
        self.assertFalse((self.home / "Library/LaunchAgents").exists())

    def test_missing_saved_cli_has_recoverable_native_error(self):
        (self.data / "settings.json").write_text(json.dumps({"codex": "/missing/codex"}))
        with self.assertRaises(bundle_install.SetupError) as caught:
            bundle_install.setup(self.args())
        self.assertEqual(caught.exception.code, "codex_missing")
        bundle_install.setup(self.args(codex=str(self.codex)))
        self.assertEqual(json.loads((self.data / "settings.json").read_text())["codex"], str(self.codex))

    def test_installed_setup_recognizes_a_symlinked_home(self):
        alias = self.home / "Home Alias"
        alias.symlink_to(self.home, target_is_directory=True)
        with patch("pathlib.Path.home", return_value=alias):
            bundle_install.setup(self.args(codex=str(self.codex), no_start=True))
        config = plistlib.loads((self.home / "Library/LaunchAgents/com.nickcorin.tokenmaxxer.plist").read_bytes())
        self.assertEqual(config["ProgramArguments"][0], str(self.runtime.executable))


if __name__ == "__main__":
    unittest.main()

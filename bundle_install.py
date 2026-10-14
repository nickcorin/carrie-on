"""First-launch setup for the immutable, self-contained Carrie On.app."""

import argparse
import fcntl
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from app_runtime import APP_IDENTIFIER, PROFILE, RUNTIME, VERSION
from app_lifecycle import agent_path, remove_agent, write_agent
from macos_compatibility import version_tuple
from legacy_migration import LegacyMigration
import carrie_on
import carrie_on_ui


MENU_LABEL = PROFILE.labels[2]
LABELS = (carrie_on.LABEL, carrie_on_ui.LABEL, MENU_LABEL)


class SetupError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Settings:
    codex: str
    port: int = PROFILE.port
    interval: int = 60
    socket: str | None = None
    codex_home: str | None = None
    schema_version: int = 1

    @classmethod
    def load(cls, args):
        path = RUNTIME.data / "settings.json"
        saved = json.loads(path.read_text()) if path.exists() else legacy_settings()
        if not isinstance(saved, dict):
            raise ValueError("settings.json must contain a JSON object")
        if saved.get("schema_version", 1) != 1:
            raise ValueError("Settings were created by a newer version of Carrie On")
        values = {name: getattr(args, name, None) if getattr(args, name, None) is not None else saved.get(name)
                  for name in ("codex", "port", "interval", "socket", "codex_home")}
        values["codex"] = resolve_codex(values["codex"])
        values["port"] = values["port"] if values["port"] is not None else PROFILE.port
        values["interval"] = values["interval"] if values["interval"] is not None else 60
        if values["codex_home"] is None and "codex_home" not in saved:
            values["codex_home"] = os.environ.get("CODEX_HOME")
        if type(values["port"]) is not int or not 1024 <= values["port"] <= 65535:
            raise ValueError("Port must be between 1024 and 65535")
        if type(values["interval"]) is not int or values["interval"] < 30:
            raise ValueError("Interval must be at least 30 seconds")
        for name in ("socket", "codex_home"):
            if values[name] is not None:
                values[name] = str(Path(values[name]).expanduser().resolve())
        return cls(**values)


def legacy_settings():
    """Preserve connection settings from pre-0.4 services without importing queues."""
    saved = {}
    for label in (carrie_on.LABEL, carrie_on_ui.LABEL):
        path = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        if not path.exists():
            continue
        config = plistlib.loads(path.read_bytes())
        arguments = config.get("ProgramArguments", [])
        for name in ("codex", "port", "socket"):
            flag = "--" + name
            if flag in arguments:
                value = arguments[arguments.index(flag) + 1]
                saved[name] = int(value) if name == "port" else value
        if "StartInterval" in config:
            saved["interval"] = config["StartInterval"]
        if config.get("EnvironmentVariables", {}).get("CODEX_HOME"):
            saved["codex_home"] = config["EnvironmentVariables"]["CODEX_HOME"]
    return saved


def resolve_codex(requested):
    if requested:
        executable = shutil.which(requested)
        if not executable:
            raise SetupError("codex_missing", f"Codex CLI was not found at {requested}. Choose its new location.")
        return str(Path(executable).absolute())
    candidates = ["codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex",
                  str(Path.home() / ".local/bin/codex"), str(Path.home() / ".cargo/bin/codex")]
    for root in (Path.home() / ".config/nvm/versions/node", Path.home() / ".nvm/versions/node"):
        candidates.extend(str(path) for path in sorted(root.glob("*/bin/codex"), reverse=True))
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable:
            return str(Path(executable).absolute())
    raise SetupError("codex_missing", "Install and sign into the Codex CLI, or choose an existing Codex executable.")


def check_bundle():
    app = RUNTIME.app
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != APP_IDENTIFIER:
        raise ValueError("Run setup using the backend inside Carrie On.app")
    applications = {(Path.home() / "Applications").resolve(), Path("/Applications")}
    if app.parent.resolve() not in applications:
        raise SetupError("move_app", "Move Carrie On.app to Applications, then open it again.")
    metadata = json.loads((app / "Contents/Resources/release.json").read_text())
    if metadata["architecture"] != platform.machine():
        raise ValueError(f"Use the {platform.machine()} release of Carrie On")
    if version_tuple(platform.mac_ver()[0]) < version_tuple(metadata["minimum_macos"]):
        raise ValueError(f"This release requires macOS {metadata['minimum_macos']} or later")
    return metadata


def write_private(path, contents):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(contents)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def service_loaded(label):
    return subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
                          capture_output=True).returncode == 0


def stop_services(labels=LABELS):
    for label in reversed(labels):
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)


def setup(args):
    """Reconcile services with this release while preserving settings and queue data.

    The caller holds install.lock. Failures may leave stopped services; retrying repairs
    them. --no-start prepares a fresh installation without touching the launchd session.
    No setup step writes into or signs the app bundle.
    """
    metadata = check_bundle()
    if PROFILE.name == "dev" or args.no_start:
        return configure(args, metadata)
    migration = LegacyMigration(Path.home(), RUNTIME.data, LABELS)
    with migration.prepare():
        result = configure(args, metadata)
        migration.complete()
    return result


def configure(args, metadata):
    """Register the current installation after any previous data has been imported."""
    settings = Settings.load(args)
    environment = dict(os.environ)
    environment["PATH"] = f"{Path(settings.codex).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    subprocess.run([settings.codex, "--version"], check=True, env=environment,
                   capture_output=True, timeout=20)
    previous = {label: agent_path(label).read_bytes()
                for label in LABELS if agent_path(label).exists()}
    if args.no_start and previous:
        raise ValueError("--no-start requires a fresh installation")
    marker_path = RUNTIME.data / "installation.json"
    marker = json.loads(marker_path.read_text()) if marker_path.exists() else {}
    identity = {"app": str(RUNTIME.app), "build_id": metadata["build_id"], "settings": asdict(settings)}

    RUNTIME.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    if settings.codex_home:
        os.environ["CODEX_HOME"] = settings.codex_home
    else:
        os.environ.pop("CODEX_HOME", None)
    options = argparse.Namespace(command="install", state=RUNTIME.state, codex=settings.codex,
                                 socket=Path(settings.socket) if settings.socket else None,
                                 port=settings.port, interval=settings.interval, start=False)
    carrie_on.launch_agent(options)
    carrie_on_ui.install(options)
    menu = {
        "Label": MENU_LABEL,
        "ProgramArguments": ["/usr/bin/open", "-g", str(RUNTIME.app), "--args", "--background"],
        "RunAtLoad": True, "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(RUNTIME.logs / "menubar.log"),
        "StandardErrorPath": str(RUNTIME.logs / "menubar-error.log"),
    }
    write_agent(menu)
    write_private(RUNTIME.data / "settings.json", (json.dumps(asdict(settings), indent=2) + "\n").encode())
    if not args.no_start:
        changed = {label for label in LABELS if previous.get(label) != agent_path(label).read_bytes()}
        if marker != identity or args.repair:
            changed.update((carrie_on.LABEL, carrie_on_ui.LABEL))
        stop_services(tuple(label for label in LABELS if label in changed))
        if MENU_LABEL in changed:
            subprocess.run(["/usr/bin/osascript", "-e", f'if application id "{MENU_LABEL}" is running '
                            f'then tell application id "{MENU_LABEL}" to quit'], capture_output=True)
        for label in LABELS:
            if not service_loaded(label):
                subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}",
                                str(agent_path(label))], check=True, capture_output=True)
        wait_for_dashboard(settings.port, metadata["build_id"])
        write_private(marker_path, (json.dumps(identity, indent=2) + "\n").encode())
    return {"version": VERSION, "port": settings.port, "app": str(RUNTIME.app),
            "data": str(RUNTIME.data), "started": not args.no_start}


def wait_for_dashboard(port, build_id):
    for attempt in range(50):
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/health",
                                             headers={"X-Carrie-On": "1"})
            with urllib.request.urlopen(request, timeout=1) as response:
                health = json.load(response)
                if health.get("version") == VERSION and health.get("build_id") == build_id:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise ValueError(f"The dashboard did not start on port {port}. Check {RUNTIME.logs / 'dashboard-error.log'}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("setup", help="Set up or repair this app's per-user login services")
    install.add_argument("--codex", help="Codex executable; saved for future launches")
    install.add_argument("--socket", help="Custom desktop IPC socket")
    install.add_argument("--codex-home", help="Custom Codex data directory")
    install.add_argument("--port", type=int, help="Dashboard port (first-install default: 8766)")
    install.add_argument("--interval", type=int, help="Polling interval (first-install default: 60 seconds)")
    install.add_argument("--no-start", action="store_true", help="Prepare a fresh installation without loading services")
    install.add_argument("--repair", action="store_true", help="Restart the worker and dashboard")
    install.add_argument("--json", action="store_true", help="Emit a structured result or error")
    remove = commands.add_parser("remove-installation", help="Remove login services; keep the app, queue, and settings")
    remove.add_argument("--yes", action="store_true", help="Confirm removal of login services")
    args = parser.parse_args(argv)
    if args.command == "remove-installation" and not args.yes:
        parser.error("Pass --yes to confirm removal of login services")
    try:
        if sys.platform != "darwin" or not RUNTIME.bundled:
            raise ValueError("Run this command using the backend inside Carrie On.app")
        if os.getuid() == 0:
            raise ValueError("Open Carrie On as your normal user, without sudo")
        RUNTIME.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (RUNTIME.data / "install.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "setup":
                result = setup(args)
            else:
                stop_services()
                for label in LABELS:
                    remove_agent(label)
                (RUNTIME.data / "installation.json").unlink(missing_ok=True)
                result = {"message": "Login services removed. Quit Carrie On and move the app to Trash. User data is preserved."}
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as error:
        result = {"code": getattr(error, "code", "setup_failed"), "message": str(error)}
        print(json.dumps(result) if getattr(args, "json", False) else f"Carrie On setup: {error}",
              file=sys.stdout if getattr(args, "json", False) else sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

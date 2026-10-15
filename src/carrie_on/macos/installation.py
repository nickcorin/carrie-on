"""Validated, serialized installation of per-user Carrie On services."""

import argparse
import fcntl
import json
import os
import platform
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from carrie_on.macos.compatibility import version_tuple
from carrie_on.macos.legacy_migration import LegacyMigration
from carrie_on.macos.lifecycle import (
    agent_path,
    remove_agent,
    retire_worker,
    write_agent,
)
from carrie_on.macos.runtime import BuildProfile, Runtime


class SetupError(ValueError):
    """Recoverable setup failure with a stable code for the native client."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, kw_only=True)
class SettingsOverrides:
    """Per-invocation choices; absent values preserve saved configuration."""

    codex: str | None = None
    port: int | None = None
    interval: int | None = None
    socket: Path | None = None
    codex_home: Path | None = None


@dataclass(frozen=True, kw_only=True)
class Settings:
    """Validated connection settings; paths are absolute before publication."""

    codex: str
    port: int
    interval: int = 60
    socket: str | None = None
    codex_home: str | None = None
    schema_version: int = 1

    @classmethod
    def load(
        cls,
        runtime: Runtime,
        overrides: SettingsOverrides = SettingsOverrides(),
        *,
        legacy: bool = False,
    ) -> "Settings":
        """Read saved or legacy settings and apply overrides before validating.

        The installation owner calls this while holding install.lock. Relative
        input paths are resolved once against the invoking process's directory;
        the normalized values are saved so later launches cannot reinterpret them.
        Invalid settings raise ValueError before any service definition changes.
        Only installers enable legacy launchd discovery. Direct servers read their
        own settings directory and never inherit another installation's settings.
        """
        path = runtime.data / "settings.json"
        if path.exists():
            saved = json.loads(path.read_text())
        elif legacy:
            saved = legacy_settings(runtime.profile)
        else:
            saved = {}
        if not isinstance(saved, dict):
            raise ValueError("settings.json must contain a JSON object")
        if type(saved.get("schema_version", 1)) is not int or saved.get("schema_version", 1) != 1:
            raise ValueError("Settings were created by a newer version of Carrie On")
        codex = overrides.codex if overrides.codex is not None else saved.get("codex")
        port = overrides.port if overrides.port is not None else saved.get("port")
        interval = overrides.interval if overrides.interval is not None else saved.get("interval")
        socket = overrides.socket if overrides.socket is not None else saved.get("socket")
        codex_home = (
            overrides.codex_home if overrides.codex_home is not None else saved.get("codex_home")
        )
        if codex_home is None and "codex_home" not in saved:
            codex_home = os.environ.get("CODEX_HOME")
        if port is None:
            port = runtime.profile.port
        if interval is None:
            interval = 60
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("Port must be between 1024 and 65535")
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 30:
            raise ValueError("Interval must be at least 30 seconds")
        return cls(
            codex=resolve_codex(codex),
            port=port,
            interval=interval,
            socket=absolute_setting_path("socket", socket),
            codex_home=absolute_setting_path("codex_home", codex_home),
        )

    def environment(self) -> dict[str, str]:
        """Return the child environment without altering the invoking process."""
        environment = {
            "PATH": f"{Path(self.codex).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        }
        if self.codex_home:
            environment["CODEX_HOME"] = self.codex_home
        return environment


def absolute_setting_path(name: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError(f"{name} must be a nonempty path")
    return str(Path(value).expanduser().resolve())


def legacy_settings(profile: BuildProfile) -> dict:
    """Preserve connection settings from pre-0.4 services without importing queues."""
    saved = {}
    for label in (profile.service_prefix, profile.labels[0]):
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


def resolve_codex(requested: object) -> str:
    if requested is not None:
        if not isinstance(requested, str) or not requested:
            raise ValueError("codex must be an executable path")
        executable = shutil.which(requested)
        if not executable:
            raise SetupError(
                "codex_missing",
                f"Codex CLI was not found at {requested}. Choose its new location.",
            )
        return str(Path(executable).absolute())
    candidates = [
        "codex",
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
        str(Path.home() / ".local/bin/codex"),
        str(Path.home() / ".cargo/bin/codex"),
    ]
    for root in (
        Path.home() / ".config/nvm/versions/node",
        Path.home() / ".nvm/versions/node",
    ):
        candidates.extend(str(path) for path in sorted(root.glob("*/bin/codex"), reverse=True))
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable:
            return str(Path(executable).absolute())
    raise SetupError(
        "codex_missing",
        "Install and sign into the Codex CLI, or choose an existing Codex executable.",
    )


def check_bundle(runtime: Runtime) -> dict:
    app = runtime.app
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != runtime.profile.identifier:
        raise ValueError("Run setup using the backend inside Carrie On.app")
    applications = {(Path.home() / "Applications").resolve(), Path("/Applications")}
    if app.parent.resolve() not in applications:
        raise SetupError(
            "move_app",
            f"Move {runtime.profile.bundle_name} to Applications, then open it again.",
        )
    metadata = json.loads((app / "Contents/Resources/release.json").read_text())
    if metadata["architecture"] != platform.machine():
        raise ValueError(f"Use the {platform.machine()} release of Carrie On")
    if version_tuple(platform.mac_ver()[0]) < version_tuple(metadata["minimum_macos"]):
        raise ValueError(f"This release requires macOS {metadata['minimum_macos']} or later")
    return metadata


def write_private(path: Path, contents: bytes) -> None:
    """Atomically replace a user-private settings or marker file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(contents)
    temporary.chmod(0o600)
    os.replace(temporary, path)


@contextmanager
def installation_lock(runtime: Runtime) -> Iterator[None]:
    """Serialize installation and expose storage failures as recoverable setup errors."""
    runtime.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (runtime.data / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        except sqlite3.Error as error:
            raise SetupError(
                "storage_unavailable",
                f"Could not access the queue database at {runtime.state}: {error}. "
                "Keep the data and recovery copies, resolve the storage problem, and retry setup.",
            ) from error


def service_loaded(label: str) -> bool:
    return (
        subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
        ).returncode
        == 0
    )


def stop_services(labels: tuple[str, ...]) -> None:
    """Stop selected writers and wait until launchd releases their registrations."""
    for label in reversed(labels):
        subprocess.run(
            ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
        )
        for _ in range(100):
            if not service_loaded(label):
                break
            time.sleep(0.1)
        else:
            raise ValueError(f"The service {label} has not stopped. Retry setup.")


def import_source_queue(runtime: Runtime) -> None:
    """Import a stopped release source installation's immutable JSON snapshot once.

    The caller owns install.lock and has stopped the retired worker and backend.
    Development profiles never adopt the release queue. The original JSON and a
    private snapshot remain available for recovery; a transaction marker prevents
    retrying setup from reactivating completed items.
    """
    if runtime.bundled or runtime.profile.name != "release":
        return
    source = runtime.resources / "resume-queue.json"
    snapshot = runtime.data / "Migration/source-checkout/resume-queue.json"
    if not source.exists() and not snapshot.exists():
        return
    if not snapshot.exists():
        with source.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            write_private(snapshot, source.read_bytes())
    from carrie_on.storage import QueueStore

    store = QueueStore(runtime.state)
    store.initialize(runtime.data / "resume-queue.json")
    store.import_legacy_snapshot(snapshot, "source_checkout_import")


def register_backend(runtime: Runtime, settings: Settings) -> Path:
    """Publish settings and the backend definition under the caller's install lock.

    Settings are published first, so a restarted native client discovers the same
    endpoint as launchd. A failure can leave partial registration; repeating setup
    reconciles it. This operation does not start or stop processes.
    """
    runtime.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = runtime.command("server") + [
        "--state",
        str(runtime.state),
        "--codex",
        settings.codex,
        "--port",
        str(settings.port),
        "--interval",
        str(settings.interval),
    ]
    if settings.socket:
        command += ["--socket", settings.socket]
    command += ["serve"]
    write_private(
        runtime.data / "settings.json",
        (json.dumps(asdict(settings), indent=2) + "\n").encode(),
    )
    return write_agent(
        {
            "Label": runtime.profile.labels[0],
            "ProgramArguments": command,
            "WorkingDirectory": str(runtime.data),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "EnvironmentVariables": settings.environment(),
            "StandardOutPath": str(runtime.logs / "server.log"),
            "StandardErrorPath": str(runtime.logs / "server-error.log"),
        },
        runtime.profile,
    )


def verify_codex(settings: Settings) -> None:
    environment = dict(os.environ)
    environment.pop("CODEX_HOME", None)
    environment.update(settings.environment())
    subprocess.run(
        [settings.codex, "--version"],
        check=True,
        env=environment,
        capture_output=True,
        timeout=20,
    )


def install_service(
    runtime: Runtime,
    overrides: SettingsOverrides = SettingsOverrides(),
    *,
    start: bool = True,
) -> dict:
    """Install the selected backend, preserving settings, queue, and login policy.

    Repeated calls converge to the same configuration. Configuration validation
    precedes service changes; partial publication or launch failure is repaired by
    repeating this call. With start=False, only settings and definitions change.
    """
    with installation_lock(runtime):
        settings = Settings.load(runtime, overrides, legacy=True)
        verify_codex(settings)
        if start:
            retire_worker(profile=runtime.profile)
            stop_services((runtime.profile.labels[0],))
            import_source_queue(runtime)
        path = register_backend(runtime, settings)
        if start:
            subprocess.run(
                ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
                check=True,
            )
    return {"port": settings.port, "data": str(runtime.data), "started": start}


def setup(
    runtime: Runtime,
    overrides: SettingsOverrides = SettingsOverrides(),
    *,
    no_start: bool = False,
    repair: bool = False,
) -> dict:
    """Reconcile a frozen app's services without changing its signed bundle.

    Holds install.lock across migration, settings validation, and publication.
    --no-start prepares a fresh installation without touching launchd. Failures
    may leave stopped services; rerunning setup repairs them and preserves data.
    """
    metadata = check_bundle(runtime)
    with installation_lock(runtime):
        if runtime.profile.name == "dev" or no_start:
            settings = Settings.load(runtime, overrides, legacy=True)
            verify_codex(settings)
            return configure(runtime, settings, metadata, no_start=no_start, repair=repair)
        retire_worker(profile=runtime.profile, remove_definition=False)
        migration = LegacyMigration(Path.home(), runtime.data, runtime.profile.labels)
        with migration.prepare():
            settings = Settings.load(runtime, overrides, legacy=True)
            verify_codex(settings)
            result = configure(runtime, settings, metadata, no_start=no_start, repair=repair)
            migration.complete()
        return result


def configure(
    runtime: Runtime,
    settings: Settings,
    metadata: dict,
    *,
    no_start: bool,
    repair: bool,
) -> dict:
    profile = runtime.profile
    backend_label, menu_label = profile.labels
    previous = {
        label: agent_path(label, profile).read_bytes()
        for label in profile.labels
        if agent_path(label, profile).exists()
    }
    if no_start and previous:
        raise ValueError("--no-start requires a fresh installation")
    marker_path = runtime.data / "installation.json"
    marker = json.loads(marker_path.read_text()) if marker_path.exists() else {}
    identity = {
        "app": str(runtime.app),
        "build_id": metadata["build_id"],
        "settings": asdict(settings),
    }
    if not no_start:
        retire_worker(profile=profile)
    register_backend(runtime, settings)
    write_agent(
        {
            "Label": menu_label,
            "ProgramArguments": [
                "/usr/bin/open",
                "-g",
                str(runtime.app),
                "--args",
                "--background",
            ],
            "RunAtLoad": True,
            "LimitLoadToSessionType": "Aqua",
            "StandardOutPath": str(runtime.logs / "menubar.log"),
            "StandardErrorPath": str(runtime.logs / "menubar-error.log"),
        },
        profile,
    )
    if not no_start:
        changed = {
            label
            for label in profile.labels
            if previous.get(label) != agent_path(label, profile).read_bytes()
        }
        if marker != identity or repair:
            changed.add(backend_label)
        stop_services(tuple(label for label in profile.labels if label in changed))
        if menu_label in changed:
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    f'if application id "{menu_label}" is running '
                    f'then tell application id "{menu_label}" to quit',
                ],
                capture_output=True,
            )
        for label in profile.labels:
            if not service_loaded(label):
                subprocess.run(
                    [
                        "/bin/launchctl",
                        "bootstrap",
                        f"gui/{os.getuid()}",
                        str(agent_path(label, profile)),
                    ],
                    check=True,
                    capture_output=True,
                )
        wait_for_dashboard(runtime, settings.port, metadata["build_id"])
        write_private(marker_path, (json.dumps(identity, indent=2) + "\n").encode())
    return {
        "version": runtime.version,
        "port": settings.port,
        "app": str(runtime.app),
        "data": str(runtime.data),
        "started": not no_start,
    }


def wait_for_dashboard(runtime: Runtime, port: int, build_id: str) -> None:
    for _ in range(50):
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/health", headers={"X-Carrie-On": "1"}
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                health = json.load(response)
                if health.get("version") == runtime.version and health.get("build_id") == build_id:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise ValueError(
        f"The server did not start on port {port}. Check {runtime.logs / 'server-error.log'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser(
        "setup", help="Set up or repair this app's per-user login services"
    )
    install.add_argument("--codex", help="Codex executable; saved for future launches")
    install.add_argument("--socket", type=Path, help="Custom desktop IPC socket")
    install.add_argument("--codex-home", type=Path, help="Custom Codex data directory")
    install.add_argument(
        "--port", type=int, help="Dashboard port (first-install default: profile port)"
    )
    install.add_argument(
        "--interval",
        type=int,
        help="Polling interval (first-install default: 60 seconds)",
    )
    install.add_argument(
        "--no-start",
        action="store_true",
        help="Prepare a fresh installation without loading services",
    )
    install.add_argument("--repair", action="store_true", help="Restart the worker and dashboard")
    install.add_argument("--json", action="store_true", help="Emit a structured result or error")
    remove = commands.add_parser(
        "remove-installation",
        help="Remove login services; keep the app, queue, and settings",
    )
    remove.add_argument("--yes", action="store_true", help="Confirm removal of login services")
    args = parser.parse_args(argv)
    if args.command == "remove-installation" and not args.yes:
        parser.error("Pass --yes to confirm removal of login services")
    try:
        runtime = Runtime.current(registered=True)
        if sys.platform != "darwin" or not runtime.bundled:
            raise ValueError("Run this command using the backend inside Carrie On.app")
        if os.getuid() == 0:
            raise ValueError("Open Carrie On as your normal user, without sudo")
        if args.command == "setup":
            overrides = SettingsOverrides(
                codex=args.codex,
                socket=args.socket,
                codex_home=args.codex_home,
                port=args.port,
                interval=args.interval,
            )
            result = setup(runtime, overrides, no_start=args.no_start, repair=args.repair)
        else:
            with installation_lock(runtime):
                stop_services(runtime.profile.labels)
                retire_worker(profile=runtime.profile)
                for label in runtime.profile.labels:
                    remove_agent(label, runtime.profile)
                (runtime.data / "installation.json").unlink(missing_ok=True)
            result = {
                "message": "Login services removed. Quit Carrie On and move the app to Trash. User data is preserved."
            }
        print(json.dumps(result, indent=2))
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.SubprocessError,
    ) as error:
        result = {"code": getattr(error, "code", "setup_failed"), "message": str(error)}
        print(
            json.dumps(result) if getattr(args, "json", False) else f"Carrie On setup: {error}",
            file=sys.stdout if getattr(args, "json", False) else sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 130

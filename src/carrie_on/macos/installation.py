"""Install Carrie On services for the current user under an installation lock."""

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
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Iterator

from carrie_on.configuration import Settings, SettingsOverrides
from carrie_on.macos.compatibility import version_tuple
from carrie_on.macos.lifecycle import (
    agent_path,
    remove_agent,
    service_loaded,
    unregister_services,
    write_agent,
)
from carrie_on.macos.runtime import Runtime


class SetupError(ValueError):
    """Report a setup failure with a code the native client can handle."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


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
    """Replace a settings or marker file with permissions set to 0600.

    The caller must hold install.lock because writes share a temporary filename.
    Replacement is atomic, but the file and directory are not synced to disk.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(contents)
    temporary.chmod(0o600)
    os.replace(temporary, path)


@contextmanager
def installation_lock(runtime: Runtime) -> Iterator[None]:
    """Serialize settings and launchd registration changes for this installation."""
    runtime.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (runtime.data / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def register_backend(runtime: Runtime, settings: Settings) -> Path:
    """Write settings and the backend's launchd definition while the caller holds install.lock.

    Settings and the service definition are separate writes. A failure can leave
    new settings alongside the previous definition. Retry after resolving the error.
    This function does not start or stop processes.
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
            "KeepAlive": {"SuccessfulExit": False},
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
    """Install the backend while preserving the queue and existing login settings.

    Validate settings before changing services. With start=True, replace the
    backend registration and launch it. With start=False, write settings and
    service definitions without changing running services.
    """
    with installation_lock(runtime):
        settings = Settings.load(runtime.data, overrides, default_port=runtime.profile.port)
        settings = replace(
            settings, codex=resolve_codex(None if settings.codex == "codex" else settings.codex)
        )
        verify_codex(settings)
        if start:
            unregister_services((runtime.profile.labels[0],))
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
    """Set up the packaged app's services without changing its signed bundle.

    Hold install.lock while validating settings and writing service
    definitions. --no-start prepares a fresh installation without loading services.
    A failure may leave services stopped or only some files updated. After resolving
    the error, setup can be retried using the saved data.
    """
    metadata = check_bundle(runtime)
    with installation_lock(runtime):
        settings = Settings.load(runtime.data, overrides, default_port=runtime.profile.port)
        settings = replace(
            settings, codex=resolve_codex(None if settings.codex == "codex" else settings.codex)
        )
        verify_codex(settings)
        return configure(runtime, settings, metadata, no_start=no_start, repair=repair)


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
        unregister_services(tuple(label for label in profile.labels if label in changed))
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
    install.add_argument("--codex", help="Save this Codex executable for future launches")
    install.add_argument("--socket", type=Path, help="Use this Codex desktop socket")
    install.add_argument("--codex-home", type=Path, help="Use this Codex data directory")
    install.add_argument(
        "--port",
        type=int,
        help="Use this API port. A new installation defaults to its profile's port.",
    )
    install.add_argument(
        "--interval",
        type=int,
        help="Set the polling interval in seconds. A new installation defaults to 60.",
    )
    install.add_argument(
        "--no-start",
        action="store_true",
        help="Prepare a fresh installation without loading services",
    )
    install.add_argument(
        "--repair",
        action="store_true",
        help="Restart the backend even if its settings are unchanged",
    )
    install.add_argument("--json", action="store_true", help="Print the result or error as JSON")
    remove = commands.add_parser(
        "remove-installation",
        help="Remove login services and keep the app, queue, and settings",
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
                unregister_services(runtime.profile.labels)
                for label in runtime.profile.labels:
                    remove_agent(label, runtime.profile)
                (runtime.data / "installation.json").unlink(missing_ok=True)
            result = {
                "message": "Login services removed. Quit Carrie On and move the app to Trash. User data is preserved."
            }
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        result = {"code": getattr(error, "code", "setup_failed"), "message": str(error)}
        print(
            json.dumps(result) if getattr(args, "json", False) else f"Carrie On setup: {error}",
            file=sys.stdout if getattr(args, "json", False) else sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 130

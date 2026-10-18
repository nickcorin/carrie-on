#!/usr/bin/env python3
"""Build and install the native Carrie On menu bar app and login service."""

import argparse
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from carrie_on.macos.lifecycle import write_agent
from carrie_on.macos.runtime import MINIMUM_MACOS, Runtime
from tools.build_menu import build_menu

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.nickcorin.carrieon.menu"


def install(runtime: Runtime, port: int, start: bool = True) -> None:
    """Install the app and login plist; start=False leaves process lifecycle to the caller."""
    executable = (
        runtime.resources / "CarrieOnMenu"
        if runtime.bundled
        else build_menu(runtime.logs / "menubar-build")
    )
    runtime.logs.mkdir(mode=0o700, parents=True, exist_ok=True)

    app = Path.home() / "Applications" / "Carrie On Menu.app"
    if app.exists():
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != LABEL:
            raise ValueError("A different Carrie On Menu.app already exists")

    target = f"gui/{os.getuid()}/{LABEL}"
    if start:
        subprocess.run(["launchctl", "bootout", target], capture_output=True)
        # A manually opened instance is not owned by launchd; ask it to quit first.
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                f'if application id "{LABEL}" is running then tell application id "{LABEL}" to quit',
            ],
            capture_output=True,
        )
    write_menu_app(runtime, executable, app, port)

    configuration = {
        "Label": LABEL,
        "ProgramArguments": [str(app / "Contents/MacOS/CarrieOnMenu"), "--background"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",
        "EnvironmentVariables": {
            "CARRIE_ON_MENU_DIAGNOSTICS": str(runtime.logs / "menu-status.json")
        },
        "StandardOutPath": str(runtime.logs / "menubar.log"),
        "StandardErrorPath": str(runtime.logs / "menubar-error.log"),
    }
    agent = write_agent(configuration, runtime.profile)
    if start:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(agent)], check=True)
    print(f"Installed {app}")


def write_menu_app(runtime: Runtime, executable: Path, app: Path, port: int) -> None:
    """Assemble and sign a menu bundle without changing running services."""
    binary_directory = app / "Contents/MacOS"
    binary_directory.mkdir(parents=True, exist_ok=True)
    staged_binary = binary_directory / "CarrieOnMenu.new"
    shutil.copy2(executable, staged_binary)
    os.replace(staged_binary, binary_directory / "CarrieOnMenu")
    info = {
        "CFBundleIdentifier": LABEL,
        "CFBundleName": runtime.profile.app_name,
        "CFBundleDisplayName": runtime.profile.app_name,
        "CFBundleExecutable": "CarrieOnMenu",
        "CFBundleIconFile": "CarrieOn.icns",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": runtime.version,
        "LSUIElement": True,
        "LSMinimumSystemVersion": MINIMUM_MACOS,
        "NSHighResolutionCapable": True,
        "CarrieOnPort": str(port),
    }
    if not runtime.bundled:
        info["CarrieOnSourceRoot"] = str(ROOT)
        info["CarrieOnBuildPython"] = str(runtime.executable)
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "assets/CarrieOn.icns", resources / "CarrieOn.icns")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    print("Signing and verifying menu app…", flush=True)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def prepare_app(app: Path, port: int) -> None:
    """Build a fresh replacement; never install or stop the running app."""
    runtime = Runtime.current()
    app.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="carrie-on-build-") as temporary:
        executable = build_menu(Path(temporary))
        write_menu_app(runtime, executable, app, port)
    print(f"Build ready: {app}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766, help="Local API port")
    parser.add_argument(
        "--prepare-app",
        type=Path,
        help="Build a new app at this path without installing or restarting",
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    if args.prepare_app:
        prepare_app(args.prepare_app, args.port)
    else:
        install(Runtime.current(), args.port)

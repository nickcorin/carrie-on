#!/usr/bin/env python3
"""Build and install the native Carrie On menu bar app and login service."""

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path
from app_runtime import APP_NAME, MINIMUM_MACOS, RUNTIME, VERSION
from app_lifecycle import write_agent


ROOT = Path(__file__).resolve().parent
LABEL = "com.nickcorin.tokenmaxxer.menu"


def build_menu(build):
    build.mkdir(parents=True, exist_ok=True)
    # Compile stable inputs even when the working copy is being edited.
    for name in ("Brand.swift", "ChatPicker.swift", "AppSetup.swift", "AppLifecycle.swift", "AppUpdate.swift", "main.swift"):
        shutil.copyfile(ROOT / "menubar" / name, build / name)
    executable = build / "TokenmaxxerMenu"
    target = f"{platform.machine()}-apple-macosx{MINIMUM_MACOS}"
    subprocess.run(["/usr/bin/swiftc", "-O", "-target", target,
                    "-module-cache-path", str(build / "cache"),
                    "-o", str(executable), str(build / "ChatPicker.swift"), str(build / "AppSetup.swift"),
                    str(build / "AppLifecycle.swift"), str(build / "AppUpdate.swift"),
                    str(build / "Brand.swift"), str(build / "main.swift")], check=True)
    subprocess.run([str(executable), "--self-test"], check=True)
    return executable


def install(port, start=True):
    """Install the app and login plist; start=False leaves process lifecycle to the caller."""
    executable = (RUNTIME.resources / "TokenmaxxerMenu" if RUNTIME.bundled else
                  build_menu(RUNTIME.logs / "menubar-build"))
    RUNTIME.logs.mkdir(mode=0o700, parents=True, exist_ok=True)

    app = Path.home() / "Applications" / "Carrie On Menu.app"
    previous = app.with_name("Tokenmaxxer Menu.app")
    if not app.exists() and previous.exists():
        info = plistlib.loads((previous / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") == LABEL:
            previous.rename(app)
    if app.exists():
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != LABEL:
            raise ValueError("A different Carrie On Menu.app already exists")

    target = f"gui/{os.getuid()}/{LABEL}"
    if start:
        subprocess.run(["launchctl", "bootout", target], capture_output=True)
        # A manually opened instance is not owned by launchd; ask it to quit first.
        subprocess.run(["/usr/bin/osascript", "-e",
                        f'if application id "{LABEL}" is running then tell application id "{LABEL}" to quit'],
                       capture_output=True)
    binary_directory = app / "Contents/MacOS"
    binary_directory.mkdir(parents=True, exist_ok=True)
    staged_binary = binary_directory / "TokenmaxxerMenu.new"
    shutil.copy2(executable, staged_binary)
    os.replace(staged_binary, binary_directory / "TokenmaxxerMenu")
    info = {
        "CFBundleIdentifier": LABEL, "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME, "CFBundleExecutable": "TokenmaxxerMenu",
        "CFBundleIconFile": "CarrieOn.icns",
        "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
        "CFBundleShortVersionString": VERSION, "LSUIElement": True,
        "LSMinimumSystemVersion": MINIMUM_MACOS,
        "NSHighResolutionCapable": True, "TokenmaxxerPort": str(port),
    }
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "assets/CarrieOn.icns", resources / "CarrieOn.icns")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)

    configuration = {
        "Label": LABEL, "ProgramArguments": [str(binary_directory / "TokenmaxxerMenu"), "--background"],
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",
        "EnvironmentVariables": {"TOKENMAXXER_MENU_DIAGNOSTICS": str(RUNTIME.logs / "menu-status.json")},
        "StandardOutPath": str(RUNTIME.logs / "menubar.log"),
        "StandardErrorPath": str(RUNTIME.logs / "menubar-error.log"),
    }
    agent = write_agent(configuration)
    if start:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(agent)], check=True)
    print(f"Installed {app}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766, help="Local API port")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    install(args.port)

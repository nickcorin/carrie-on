"""Compile and check the native menu executable for source and bundled installs."""

import platform
import shutil
import subprocess
from pathlib import Path

from carrie_on.macos.runtime import MINIMUM_MACOS

ROOT = Path(__file__).resolve().parents[1]


def build_menu(build: Path) -> Path:
    """Return the executable in build after compilation and self-tests succeed."""
    print("Compiling menu app…", flush=True)
    build.mkdir(parents=True, exist_ok=True)
    # Compile copied inputs so later working-copy edits cannot change them.
    for name in (
        "Brand.swift",
        "ChatPicker.swift",
        "AppSetup.swift",
        "AppLifecycle.swift",
        "AppUpdate.swift",
        "main.swift",
    ):
        shutil.copyfile(ROOT / "menubar" / name, build / name)
    executable = build / "CarrieOnMenu"
    target = f"{platform.machine()}-apple-macosx{MINIMUM_MACOS}"
    subprocess.run(
        [
            "/usr/bin/swiftc",
            "-O",
            "-target",
            target,
            "-module-cache-path",
            str(build / "cache"),
            "-o",
            str(executable),
            str(build / "ChatPicker.swift"),
            str(build / "AppSetup.swift"),
            str(build / "AppLifecycle.swift"),
            str(build / "AppUpdate.swift"),
            str(build / "Brand.swift"),
            str(build / "main.swift"),
        ],
        check=True,
    )
    print("Running menu self-tests…", flush=True)
    subprocess.run([str(executable), "--self-test"], check=True)
    return executable

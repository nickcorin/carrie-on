"""Build an isolated picker for accessibility checks; it never starts Carrie On services."""

import argparse
import platform
import plistlib
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_preview(output: Path) -> Path:
    """Build a separately identified app using the real picker and 150 synthetic chats."""
    app = output / "Carrie On Scroll Fixture.app"
    executable = app / "Contents/MacOS/ScrollFixture"
    executable.parent.mkdir(parents=True, exist_ok=True)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.nickcorin.carrieon.scroll-fixture",
                "CFBundleName": "Carrie On Scroll Fixture",
                "CFBundleExecutable": "ScrollFixture",
                "CFBundlePackageType": "APPL",
                "LSMinimumSystemVersion": "13.0",
            }
        )
    )
    with tempfile.TemporaryDirectory(prefix="carrie-scroll-build-") as temporary:
        build = Path(temporary)
        declarations = (
            (ROOT / "menubar/main.swift")
            .read_text()
            .split('if CommandLine.arguments.contains("--self-test") {', 1)[0]
        )
        main = build / "main.swift"
        main.write_text(declarations + (ROOT / "tests/ChatScrollPreview.swift").read_text())
        sources = [
            path for path in sorted((ROOT / "menubar").glob("*.swift")) if path.name != "main.swift"
        ]
        subprocess.run(
            [
                "/usr/bin/swiftc",
                "-target",
                f"{platform.machine()}-apple-macosx13.0",
                "-module-cache-path",
                str(build / "cache"),
                "-o",
                str(executable),
                *map(str, sources),
                str(main),
            ],
            check=True,
        )
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run the bounded accessibility regression (requires a macOS desktop session).",
    )
    parser.add_argument(
        "--snapshots", type=Path, help="Save light/dark picker images during verification."
    )
    args = parser.parse_args()
    app = build_preview(args.output).resolve()
    print(app, flush=True)
    if args.verify:
        command = [str(app / "Contents/MacOS/ScrollFixture"), "--verify-scrolling"]
        if args.snapshots:
            command.extend(["--snapshots", str(args.snapshots.resolve())])
        subprocess.run(
            command,
            check=True,
            timeout=45,
        )

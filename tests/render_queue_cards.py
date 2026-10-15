"""Render native queue-card fixtures: python3 tests/render_queue_cards.py OUTPUT.png [--dark]."""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render(output, dark=False):
    """Compile isolated native fixtures and save a PNG without reading live state."""
    with tempfile.TemporaryDirectory(prefix="carrie-card-preview-") as temporary:
        build = Path(temporary)
        declarations = (
            (ROOT / "menubar/main.swift")
            .read_text()
            .split('if CommandLine.arguments.contains("--self-test") {', 1)[0]
        )
        main = build / "main.swift"
        main.write_text(declarations + (ROOT / "tests/QueueCardPreview.swift").read_text())
        executable = build / "preview"
        sources = [
            ROOT / "menubar" / name
            for name in (
                "ChatPicker.swift",
                "AppSetup.swift",
                "AppLifecycle.swift",
                "AppUpdate.swift",
                "Brand.swift",
            )
        ]
        subprocess.run(
            [
                "/usr/bin/swiftc",
                "-module-cache-path",
                str(build / "cache"),
                "-o",
                str(executable),
                *map(str, sources),
                str(main),
            ],
            check=True,
        )
        subprocess.run(
            [str(executable), str(Path(output).resolve()), *(["--dark"] if dark else [])],
            check=True,
        )


if __name__ == "__main__":
    render(sys.argv[1], "--dark" in sys.argv[2:])

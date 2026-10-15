"""Export Swift's native symbol reference: python -m tools.export_native_reference."""

import argparse
import json
import platform
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def export_reference(output: Path) -> list[Path]:
    """Compile the menu app for macOS 13 and export internal interfaces with their documentation.

    The output contains official Swift symbol graphs, suitable for DocC or other
    documentation readers. Compilation uses a temporary module and cache;
    it does not launch the app, install a bundle, or contact the local service.
    """
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="carrie-native-reference-") as temporary:
        build = Path(temporary)
        subprocess.run(
            [
                "/usr/bin/swiftc",
                "-target",
                f"{platform.machine()}-apple-macosx13.0",
                "-module-name",
                "CarrieOnMenu",
                "-module-cache-path",
                str(build / "cache"),
                "-emit-module",
                "-emit-module-path",
                str(build / "CarrieOnMenu.swiftmodule"),
                "-emit-symbol-graph",
                "-emit-symbol-graph-dir",
                str(output),
                "-symbol-graph-minimum-access-level",
                "internal",
                "-symbol-graph-pretty-print",
                *map(str, sorted((ROOT / "menubar").glob("*.swift"))),
            ],
            check=True,
        )
    graphs = sorted(output.glob("CarrieOnMenu*.symbols.json"))
    if not graphs:
        raise RuntimeError("Swift did not produce a native symbol reference")
    return graphs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/reference/native")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("Native references require the macOS Swift toolchain.")
    for path in export_reference(args.output):
        graph = json.loads(path.read_text())
        print(f"{path}: {len(graph['symbols'])} symbols")


if __name__ == "__main__":
    main()

"""Export Swift's native symbol reference: python -m tools.export_native_reference."""

import argparse
import json
import platform
import subprocess
import tempfile
from html import escape
from pathlib import Path
from urllib.parse import unquote, urlsplit

from tools.export_reference import check_links

ROOT = Path(__file__).resolve().parents[1]


def export_reference(output: Path) -> list[Path]:
    """Compile the menu app for macOS 13 and export internal interfaces with their documentation.

    The output contains Swift symbol graphs and an HTML index of app declarations.
    Compilation uses a temporary module and cache;
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
    symbols = {}
    for path in graphs:
        for symbol in json.loads(path.read_text())["symbols"]:
            source = Path(unquote(urlsplit(symbol.get("location", {}).get("uri", "")).path))
            if source.parent == ROOT / "menubar":
                symbols[symbol["identifier"]["precise"]] = symbol
    links, sections = [], []
    for index, symbol in enumerate(
        sorted(symbols.values(), key=lambda item: item["pathComponents"])
    ):
        name = escape(".".join(symbol["pathComponents"]))
        declaration = escape("".join(part["spelling"] for part in symbol["declarationFragments"]))
        documentation = escape(
            "\n".join(line["text"] for line in symbol.get("docComment", {}).get("lines", []))
        )
        links.append(f'<li><a href="#symbol-{index}">{name}</a></li>')
        sections.append(
            f'<section id="symbol-{index}"><h2>{name}</h2><pre>{declaration}</pre><p>{documentation}</p></section>'
        )
    (output / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Carrie On native reference</title>'
        "<style>body{font:16px/1.5 system-ui;max-width:75rem;margin:2rem auto;padding:0 2rem}"
        "pre{white-space:pre-wrap;background:#eee;padding:1rem}p{white-space:pre-wrap}"
        "section{border-top:1px solid #aaa;margin-top:2rem}a{color:#1659a3}</style>"
        "<h1>Carrie On native reference</h1><p>App declarations compiled for macOS 13. "
        "Inherited framework members are omitted.</p><ul>"
        + "".join(f'<li><a href="{path.name}">{path.name}</a></li>' for path in graphs)
        + f"</ul><details><summary>Browse {len(symbols)} declarations</summary><nav><ul>"
        + "".join(links)
        + "</ul></nav></details>"
        + "".join(sections)
        + "</html>"
    )
    check_links(output)
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

"""Generate the Python interface reference without starting processes or opening state."""

import argparse
import importlib
import pydoc
from pathlib import Path

MODULES = (
    "queue",
    "ports",
    "storage.queue",
    "storage.database",
    "storage.schema",
    "legacy",
    "codex.models",
    "codex.client",
    "codex.sdk",
    "application",
    "dispatch",
    "api",
    "server",
    "macos.runtime",
    "macos.installation",
)


def export(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    links = []
    for name in MODULES:
        qualified = "carrie_on." + name
        module = importlib.import_module(qualified)
        page = pydoc.HTMLDoc().page(qualified, pydoc.HTMLDoc().docmodule(module))
        (output / f"{qualified}.html").write_text(page)
        links.append(f'<li><a href="{qualified}.html">{qualified}</a></li>')
    (output / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Carrie On Python reference</title>'
        "<h1>Carrie On Python reference</h1><ul>" + "".join(links) + "</ul></html>"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/reference/python"))
    export(parser.parse_args().output)


if __name__ == "__main__":
    main()

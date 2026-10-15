"""Generate the Python interface reference without starting processes or opening state."""

import argparse
import importlib
import pkgutil
import pydoc
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


class PageLinks(HTMLParser):
    def __init__(self, source: str):
        super().__init__()
        self.targets: list[str] = []
        self.anchors: set[str] = set()
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a" and "href" in attributes:
            self.targets.append(attributes["href"])
        for name in ("id", "name"):
            if name in attributes:
                self.anchors.add(attributes[name])


def check_links(output: Path) -> None:
    """Reject missing local pages and fragments in generated HTML."""
    pages = {path.resolve(): PageLinks(path.read_text()) for path in output.rglob("*.html")}
    for path, page in pages.items():
        for target in page.targets:
            parts = urlsplit(target)
            if parts.scheme or parts.netloc:
                continue
            linked = (path.parent / unquote(parts.path)).resolve() if parts.path else path
            if not linked.is_file():
                raise ValueError(f"Broken reference link in {path.name}: {target}")
            if parts.fragment and linked in pages:
                if unquote(parts.fragment) not in pages[linked].anchors:
                    raise ValueError(f"Missing reference anchor in {path.name}: {target}")


def export(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    package = importlib.import_module("carrie_on")
    modules = [package.__name__] + sorted(
        module.name
        for module in pkgutil.walk_packages(package.__path__, package.__name__ + ".")
        if not module.name.endswith(".__main__")
    )

    def portable_link(match: re.Match[str]) -> str:
        target, label = match.groups()
        parts = urlsplit(unescape(target))
        if target == ".":
            return f'<a href="index.html">{label}</a>'
        if parts.scheme == "file":
            return label
        if not parts.scheme and parts.path.endswith(".html"):
            if parts.path.removesuffix(".html") not in modules:
                return label
        return match.group()

    links = []
    for qualified in modules:
        module = importlib.import_module(qualified)
        page = pydoc.HTMLDoc().page(qualified, pydoc.HTMLDoc().docmodule(module))
        page = re.sub(r'<a href="([^"]+)">(.*?)</a>', portable_link, page, flags=re.DOTALL)
        (output / f"{qualified}.html").write_text(page)
        links.append(f'<li><a href="{qualified}.html">{qualified}</a></li>')
    (output / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Carrie On Python reference</title>'
        "<h1>Carrie On Python reference</h1><ul>" + "".join(links) + "</ul></html>"
    )
    check_links(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/reference/python"))
    export(parser.parse_args().output)


if __name__ == "__main__":
    main()

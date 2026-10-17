#!/usr/bin/env python3
"""Generate the app and tray artwork from the shared vector silhouette."""

import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


def generate_vectors():
    """Compile the canonical absolute SVG path into AppKit and colored SVG assets."""
    svg = ET.parse(ASSETS / "mark.svg").getroot()
    path = svg.find("{http://www.w3.org/2000/svg}path").attrib["d"]
    assert svg.attrib["viewBox"] == "0 0 380 315"
    statements = []
    for command, coordinates in re.findall(r"([A-Za-z])([^A-Za-z]*)", path):
        values = coordinates.split()
        if command == "M" and len(values) == 2:
            statements.append(f"path.move(to: NSPoint(x: {values[0]}, y: {values[1]}))")
        elif command == "L" and len(values) == 2:
            statements.append(f"path.line(to: NSPoint(x: {values[0]}, y: {values[1]}))")
        elif command == "C" and len(values) == 6:
            statements.append(
                f"path.curve(to: NSPoint(x: {values[4]}, y: {values[5]}), "
                f"controlPoint1: NSPoint(x: {values[0]}, y: {values[1]}), "
                f"controlPoint2: NSPoint(x: {values[2]}, y: {values[3]}))"
            )
        elif command == "Z" and not values:
            statements.append("path.close()")
        else:
            raise ValueError(f"Unsupported vector command: {command} {coordinates}")
    drawing = "\n".join(f"        {statement}" for statement in statements)
    (ROOT / "menubar/Brand.swift").write_text(
        """// Generated from assets/mark.svg by build_icons.py.
import AppKit

enum Brand {
    static func markPath(in bounds: NSRect) -> NSBezierPath {
        let path = NSBezierPath()
"""
        + drawing
        + """
        let scale = min(bounds.width / 380, bounds.height / 315)
        var transform = AffineTransform(translationByX: bounds.midX - 190 * scale,
                                         byY: bounds.midY - 157.5 * scale)
        transform.scale(scale)
        path.transform(using: transform)
        return path
    }

    static var trayIcon: NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: true) { bounds in
            NSColor.black.setFill()
            markPath(in: bounds).fill()
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "Carrie On"
        return image
    }
}
"""
    )
    (ASSETS / "tray-icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 380 380">\n'
        f'  <path fill="#000000" transform="translate(0 32.5)" d="{path}"/>\n</svg>\n'
    )
    (ASSETS / "app-icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">\n'
        '  <rect x="72" y="72" width="880" height="880" rx="196" fill="#354DE8"/>\n'
        f'  <path fill="#F4F4FF" transform="translate(195 249.223684) scale(1.668421052632)" '
        f'd="{path}"/>\n</svg>\n'
    )


def build_icons():
    generate_vectors()
    with tempfile.TemporaryDirectory(prefix="carrie-on-icons-") as temporary:
        work = Path(temporary)
        renderer = work / "render-icons"
        subprocess.run(
            [
                "/usr/bin/swiftc",
                "-module-cache-path",
                str(work / "cache"),
                str(ROOT / "menubar/Brand.swift"),
                str(ASSETS / "RenderIcons.swift"),
                "-o",
                str(renderer),
            ],
            check=True,
        )
        subprocess.run([str(renderer), str(ASSETS)], check=True)
        iconset = work / "CarrieOn.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = size * scale
                suffix = "@2x" if scale == 2 else ""
                subprocess.run(
                    [
                        "sips",
                        "-z",
                        str(pixels),
                        str(pixels),
                        str(ASSETS / "app-icon.png"),
                        "--out",
                        str(iconset / f"icon_{size}x{size}{suffix}.png"),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "CarrieOn.icns")],
            check=True,
        )


if __name__ == "__main__":
    build_icons()

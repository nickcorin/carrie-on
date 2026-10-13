#!/usr/bin/env python3
"""Package the Carrie On master artwork into a macOS icon and web thumbnail."""

import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def build_icons():
    source = ROOT / "assets/app-icon.png"
    with tempfile.TemporaryDirectory(prefix="carrie-on-icons-") as temporary:
        iconset = Path(temporary) / "CarrieOn.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = size * scale
                suffix = "@2x" if scale == 2 else ""
                subprocess.run(["sips", "-z", str(pixels), str(pixels), str(source), "--out",
                                str(iconset / f"icon_{size}x{size}{suffix}.png")], check=True,
                               stdout=subprocess.DEVNULL)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o",
                        str(ROOT / "assets/CarrieOn.icns")], check=True)
    subprocess.run(["sips", "-z", "128", "128", str(source), "--out",
                    str(ROOT / "web/app-icon.png")], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    build_icons()

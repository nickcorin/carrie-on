"""Build and verify the drag-to-Applications disk image alongside the updater ZIP."""

import os
import subprocess
import tempfile
import time
from pathlib import Path

from tools.verify_bundle import app_contents

ROOT = Path(__file__).resolve().parents[1]


def build_disk_image(app, image, version):
    """Package an already signed app; do not modify it or start installed services."""
    import dmgbuild

    with tempfile.TemporaryDirectory(prefix="carrie-dmg-art-") as temporary:
        work = Path(temporary)
        renderer = work / "render-background"
        background = work / "background.png"
        subprocess.run(
            [
                "/usr/bin/swiftc",
                "-module-cache-path",
                str(work / "cache"),
                str(ROOT / "assets/DMGBackground.swift"),
                "-o",
                str(renderer),
            ],
            check=True,
        )
        subprocess.run([str(renderer), str(background), "1"], check=True)
        subprocess.run([str(renderer), str(work / "background@2x.png"), "2"], check=True)
        dmgbuild.build_dmg(
            str(image),
            f"{app.stem} {version}",
            settings={
                "format": "UDZO",
                "filesystem": "HFS+",
                "files": [str(app)],
                "symlinks": {"Applications": "/Applications"},
                "background": str(background),
                "icon": str(app / "Contents/Resources/CarrieOn.icns"),
                "window_rect": ((200, 160), (640, 440)),
                "icon_size": 112,
                "text_size": 14,
                "icon_locations": {app.name: (160, 170), "Applications": (480, 170)},
                "default_view": "icon-view",
                "include_icon_view_settings": True,
                "include_list_view_settings": False,
                "show_status_bar": False,
                "show_tab_view": False,
                "show_toolbar": False,
                "show_pathbar": False,
                "show_sidebar": False,
            },
        )
    verify_disk_image(image, app)


def verify_disk_image(image, original):
    """Mount read-only, verify the install target, and copy out the identical signed app.

    Always detach the verification mount. Fail rather than accepting changed bundle
    contents or an Applications link that points somewhere else.
    """
    subprocess.run(["/usr/bin/hdiutil", "verify", str(image)], check=True, capture_output=True)
    with tempfile.TemporaryDirectory(prefix="carrie-dmg-check-") as temporary:
        root = Path(temporary)
        mount = root / "Mounted"
        mount.mkdir()
        subprocess.run(
            [
                "/usr/bin/hdiutil",
                "attach",
                str(image),
                "-readonly",
                "-nobrowse",
                "-mountpoint",
                str(mount),
            ],
            check=True,
            capture_output=True,
        )
        try:
            visible = {path.name for path in mount.iterdir() if not path.name.startswith(".")}
            if visible != {original.name, "Applications"}:
                raise ValueError(
                    "The disk image must contain only the app and Applications shortcut"
                )
            if (
                not (mount / "Applications").is_symlink()
                or os.readlink(mount / "Applications") != "/Applications"
            ):
                raise ValueError("The disk image Applications shortcut has an invalid target")
            copied = root / "Copied" / original.name
            subprocess.run(["/usr/bin/ditto", str(mount / original.name), str(copied)], check=True)
            if app_contents(copied) != app_contents(original):
                raise ValueError("The disk image app differs from the updater app")
            subprocess.run(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(copied)],
                check=True,
                capture_output=True,
            )
        finally:
            for attempt in range(5):
                result = subprocess.run(
                    ["/usr/bin/hdiutil", "detach", str(mount)], capture_output=True
                )
                if result.returncode == 0:
                    break
                if attempt == 4:
                    raise RuntimeError(
                        f"Could not detach the verification disk image: {result.stderr.decode()}"
                    )
                time.sleep(0.5)
    print("PASS: disk image integrity, Applications shortcut, copied app identity, and signature")

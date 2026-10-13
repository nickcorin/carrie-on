#!/usr/bin/env python3
"""Build, sign, verify, and archive a self-contained Carrie On.app."""

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import uuid
from importlib.metadata import distribution as package_distribution
from pathlib import Path

from app_runtime import APP_BUNDLE_NAME, APP_IDENTIFIER, APP_NAME, MINIMUM_MACOS
from install_menubar import build_menu
from macos_compatibility import audit
from verify_bundle import verify
from release import Release

ROOT = Path(__file__).resolve().parent


def build(output, replace=False):
    if sys.platform != "darwin":
        raise ValueError("Build on a Mac with Python 3.10+ and Apple's Swift command-line tools")
    release = Release.read(ROOT)
    architecture = platform.machine()
    name = f"Carrie-On-{release.version}-macos-{architecture}"
    archive = output / f"{name}.zip"
    if not replace and any((output / f"{name}{suffix}").exists() for suffix in (".zip", ".zip.sha256", ".json")):
        raise ValueError(f"Release {release.version} already exists. Bump VERSION, or use --replace for a development rebuild.")
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix="tokenmaxxer-build-") as temporary:
        work = Path(temporary)
        menu = build_menu(work / "menu")
        environment = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / "cache"))
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
            "--name", "tokenmaxxer", "--target-arch", architecture,
            "--osx-bundle-identifier", APP_IDENTIFIER,
            "--distpath", str(work / "dist"), "--workpath", str(work / "build"),
            "--specpath", str(work), "--add-data", f"{ROOT / 'web'}:web",
            "--add-data", f"{ROOT / 'VERSION'}:.", str(ROOT / "bundle_entry.py"),
        ], check=True, cwd=ROOT, env=environment)
        app = work / APP_BUNDLE_NAME
        shutil.move(str(work / "dist/tokenmaxxer.app"), app)
        contents = app / "Contents"
        native = contents / "MacOS/TokenmaxxerMenu"
        shutil.copy2(menu, native)
        info_path = contents / "Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        info.update({"CFBundleIdentifier": APP_IDENTIFIER, "CFBundleExecutable": "TokenmaxxerMenu",
                     "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME,
                     "CFBundleIconFile": "CarrieOn.icns",
                     "CFBundleVersion": release.version, "CFBundleShortVersionString": release.version,
                     "LSUIElement": True, "LSMinimumSystemVersion": MINIMUM_MACOS,
                     "NSHighResolutionCapable": True, "TokenmaxxerStandalone": True})
        info_path.write_bytes(plistlib.dumps(info))
        resources = contents / "Resources"
        shutil.copy2(ROOT / "assets/CarrieOn.icns", resources / "CarrieOn.icns")
        docs = resources / "Documentation"
        docs.mkdir()
        for filename in ("INSTALL.md", "README.md", "USAGE.md", "CHANGELOG.md",
                         "CONTRIBUTING.md", "RELEASING.md", "SECURITY.md", "LICENSE",
                         "connection-api.openapi.json", "instruction-api.openapi.json", "chat-pages.openapi.json"):
            shutil.copy2(ROOT / filename, docs / filename)
        shutil.copytree(ROOT / "features", docs / "features")
        (docs / "assets").mkdir()
        shutil.copy2(ROOT / "assets/README.md", docs / "assets/README.md")
        licenses = resources / "Licenses"
        licenses.mkdir()
        shutil.copy2(ROOT / "LICENSE", licenses / "Carrie-On.txt")
        shutil.copy2(Path(sysconfig.get_path("stdlib")) / "LICENSE.txt", licenses / "Python.txt")
        pyinstaller = package_distribution("pyinstaller")
        license_file = next(file for file in pyinstaller.files if str(file).endswith("licenses/COPYING.txt"))
        shutil.copy2(pyinstaller.locate_file(license_file), licenses / "PyInstaller.txt")
        metadata = {"version": release.version, "build_id": str(uuid.uuid4()), "architecture": architecture,
                    "minimum_macos": MINIMUM_MACOS, "built_on_macos": platform.mac_ver()[0],
                    "python": platform.python_version(), "signing": "ad-hoc",
                    "binary_audit": audit(app, MINIMUM_MACOS)}
        (resources / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
        # Sign inside out, after every resource and executable is in its final location.
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(contents / "MacOS/tokenmaxxer")], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(native)], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        staged = work / archive.name
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(staged)], check=True)
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        staged.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
        verify(staged)
        metadata.update({"archive": archive.name, "sha256": digest, "bytes": staged.stat().st_size})
        staged.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
        output.mkdir(parents=True, exist_ok=True)
        for suffix in (".zip", ".zip.sha256", ".json"):
            shutil.move(str(work / f"{name}{suffix}"), output / f"{name}{suffix}")
        print(f"Built and verified {archive}\nRequires macOS {MINIMUM_MACOS}+ ({architecture}).")
        return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist", help="Release directory")
    parser.add_argument("--replace", action="store_true", help="Overwrite this version's local release artifacts")
    args = parser.parse_args()
    try:
        build(args.output.resolve(), args.replace)
        return 0
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
        print(f"Carrie On build: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

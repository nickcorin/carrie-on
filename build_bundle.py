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

from app_runtime import MINIMUM_MACOS, PROFILES
from install_menubar import build_menu
from macos_compatibility import audit
from verify_bundle import verify
from release import Release, source_commit
from disk_image import build_disk_image
from branding import check_source, check_bundle

ROOT = Path(__file__).resolve().parent


def sign_app(app):
    """Ad-hoc sign completed executables and their enclosing app, then verify it."""
    contents = app / "Contents"
    for target in (contents / "MacOS/carrie_on", contents / "MacOS/CarrieOnMenu", app):
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(target)], check=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def build(output, replace=False, profile=PROFILES["release"], prepare_app=None):
    if prepare_app is not None:
        if profile.name != "dev":
            raise ValueError("Only dev builds can stage an app for local rebuilding")
        if prepare_app.exists():
            raise ValueError("The rebuild destination already exists")
    if sys.platform != "darwin":
        raise ValueError("Build on a Mac with Python 3.10+ and Apple's Swift command-line tools")
    release = Release.read(ROOT)
    check_source(ROOT)
    commit = source_commit(ROOT)
    dev_commit = (subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
                  if profile.name == "dev" else None)
    architecture = platform.machine()
    name = f"{profile.app_name.replace(' ', '-')}-{release.version}-macos-{architecture}"
    archive = output / f"{name}.zip"
    suffixes = (".zip", ".zip.sha256", ".json", ".dmg", ".dmg.sha256")
    if not replace and any((output / f"{name}{suffix}").exists() for suffix in suffixes):
        raise ValueError(f"Release {release.version} already exists. Bump VERSION, or use --replace for a development rebuild.")
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix="carrie_on-build-") as temporary:
        work = Path(temporary)
        profile_file = work / "build-profile.json"
        profile_file.write_text(json.dumps({"profile": profile.name}) + "\n")
        menu = build_menu(work / "menu")
        environment = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / "cache"))
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
            "--name", "carrie_on", "--target-arch", architecture,
            "--osx-bundle-identifier", profile.identifier,
            "--distpath", str(work / "dist"), "--workpath", str(work / "build"),
            "--specpath", str(work),
            "--add-data", f"{ROOT / 'VERSION'}:.",
            "--add-data", f"{profile_file}:.", str(ROOT / "bundle_entry.py"),
        ], check=True, cwd=ROOT, env=environment)
        app = work / profile.bundle_name
        shutil.move(str(work / "dist/carrie_on.app"), app)
        contents = app / "Contents"
        native = contents / "MacOS/CarrieOnMenu"
        shutil.copy2(menu, native)
        info_path = contents / "Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        info.update({"CFBundleIdentifier": profile.identifier, "CFBundleExecutable": "CarrieOnMenu",
                     "CFBundleName": profile.app_name, "CFBundleDisplayName": profile.app_name,
                     "CarrieOnDataDirectory": profile.data_directory,
                     "CarrieOnServiceLabels": list(profile.labels),
                     "CarrieOnPort": str(profile.port),
                     "CFBundleIconFile": "CarrieOn.icns",
                     "CFBundleVersion": release.version, "CFBundleShortVersionString": release.version,
                     "LSUIElement": True, "LSMinimumSystemVersion": MINIMUM_MACOS,
                     "NSHighResolutionCapable": True, "CarrieOnStandalone": True})
        info["CarrieOnBuildProfile"] = profile.name
        if profile.name == "dev":
            info["CarrieOnSourceRoot"] = str(ROOT)
            info["CarrieOnBuildPython"] = sys.executable
            info["CarrieOnSourceCommit"] = dev_commit
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
        metadata = {"version": release.version, "profile": profile.name,
                    "build_id": str(uuid.uuid4()), "architecture": architecture,
                    "minimum_macos": MINIMUM_MACOS, "built_on_macos": platform.mac_ver()[0],
                    "python": platform.python_version(), "signing": "ad-hoc", "source_commit": commit,
                    "binary_audit": audit(app, MINIMUM_MACOS)}
        (resources / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
        check_bundle(app)
        sign_app(app)
        staged = work / archive.name
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(staged)], check=True)
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        staged.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
        verify(staged, profile)
        if prepare_app is not None:
            prepare_app.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(app, prepare_app, symlinks=True)
            print(f"Build ready: {prepare_app}", flush=True)
            return prepare_app
        image = work / f"{name}.dmg"
        build_disk_image(app, image, release.version)
        image_digest = hashlib.sha256(image.read_bytes()).hexdigest()
        image.with_suffix(".dmg.sha256").write_text(f"{image_digest}  {image.name}\n")
        metadata.update({"archive": archive.name, "sha256": digest, "bytes": staged.stat().st_size})
        metadata["disk_image"] = {"file": image.name, "sha256": image_digest, "bytes": image.stat().st_size}
        staged.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
        if source_commit(ROOT) != commit:
            raise ValueError("Source changed during the build; rebuild before releasing")
        output.mkdir(parents=True, exist_ok=True)
        for suffix in suffixes:
            shutil.move(str(work / f"{name}{suffix}"), output / f"{name}{suffix}")
        print(f"Built and verified {archive}\nRequires macOS {MINIMUM_MACOS}+ ({architecture}).")
        return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist", help="Release directory")
    parser.add_argument("--replace", action="store_true", help="Overwrite this version's local release artifacts")
    parser.add_argument("--profile", choices=PROFILES, default="release", help="Isolated installation namespace")
    parser.add_argument("--prepare-app", type=Path, help="Stage a verified dev app without installing or restarting")
    parser.add_argument("--sign-app", type=Path, metavar="APP", help="Ad-hoc sign and verify an existing app instead of building")
    args = parser.parse_args()
    try:
        if args.sign_app:
            sign_app(args.sign_app.resolve())
        else:
            build(args.output.resolve(), args.replace, PROFILES[args.profile], args.prepare_app)
        return 0
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
        print(f"Carrie On build: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Exercise a release in an isolated home with no Python on PATH or live login jobs."""

import argparse
import hashlib
import json
import os
import plistlib
import re
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from macos_compatibility import audit
from app_runtime import PROFILES


def app_contents(app):
    """Fingerprint files and links to detect any first-launch mutation of the app."""
    return {str(path.relative_to(app)): (os.readlink(path) if path.is_symlink() else
            hashlib.sha256(path.read_bytes()).hexdigest())
            for path in app.rglob("*") if path.is_symlink() or path.is_file()}


def verify(archive, profile=None):
    expected = archive.with_suffix(".zip.sha256").read_text().split()[0]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected, "Archive checksum mismatch"
    with zipfile.ZipFile(archive) as bundle:
        if profile is None:
            profiles = [candidate for candidate in PROFILES.values()
                        if f"{candidate.bundle_name}/Contents/Info.plist" in bundle.namelist()]
            assert len(profiles) == 1, "Archive must contain exactly one supported app"
            profile = profiles[0]
        for name in bundle.namelist():
            parts = Path(name).parts
            assert parts[0] in {profile.bundle_name, "__MACOSX"}, name
            assert not {".runtime", ".codex", "resume-queue.json", "resume-queue.lock"}.intersection(parts), name
            assert not name.startswith("/") and ".." not in parts, name
    with tempfile.TemporaryDirectory(prefix="tokenmaxxer release check ") as temporary:
        root = Path(temporary).resolve()
        home = root / "Fresh Home"
        home.mkdir()
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(root / "Extracted")], check=True)
        extracted = root / "Extracted" / profile.bundle_name
        app = home / "Applications" / profile.bundle_name
        subprocess.run(["/usr/bin/ditto", str(extracted), str(app)], check=True)
        metadata = json.loads((app / "Contents/Resources/release.json").read_text())
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        assert info["CFBundleDisplayName"] == profile.app_name
        assert info["CFBundleIdentifier"] == profile.identifier
        assert info["CarrieOnServiceLabels"] == list(profile.labels)
        assert info["CarrieOnDataDirectory"] == profile.data_directory
        assert metadata["profile"] == profile.name
        assert (app / "Contents/Resources" / info["CFBundleIconFile"]).read_bytes()[:4] == b"icns"
        assert audit(app, metadata["minimum_macos"]) == metadata["binary_audit"]
        documentation = app / "Contents/Resources/Documentation"
        for name in ("README.md", "USAGE.md", "INSTALL.md", "RELEASING.md",
                     "CONTRIBUTING.md", "SECURITY.md", "LICENSE", "features/index.md", "assets/README.md"):
            assert (documentation / name).is_file(), f"Missing bundled documentation: {name}"
        for document in documentation.rglob("*.md"):
            for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
                if "://" in target or target.startswith(("mailto:", "#")):
                    continue
                linked = target.split("#")[0]
                assert not linked or (document.parent / linked).exists(), f"Broken bundled link: {document.name}: {target}"
        assert (app / "Contents/Resources/Licenses/Carrie-On.txt").read_text() == (documentation / "LICENSE").read_text()
        before = app_contents(app)
        cli = app / "Contents/MacOS/tokenmaxxer"
        native = app / "Contents/MacOS/TokenmaxxerMenu"
        fake_codex = root / "fake-codex"
        fake_codex.write_text('#!/bin/sh\n[ "$1" = "--version" ] || exit 99\necho "codex-cli test"\n')
        fake_codex.chmod(0o755)
        environment = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                       "TMPDIR": str(root), "LANG": "en_US.UTF-8"}
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        def run(executable, *arguments, success=True):
            process = subprocess.Popen([str(executable), *arguments], env=environment, cwd=root,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       start_new_session=True)
            try:
                stdout, stderr = process.communicate(timeout=60)
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate()
                raise
            if success and process.returncode:
                raise RuntimeError(f"{' '.join(arguments)}: {stderr}\n{stdout}")
            if not success:
                assert process.returncode != 0
            return stdout

        assert run(cli, "--version").strip() == f"tokenmaxxer {metadata['version']}"
        # The same native helper used by first launch drives setup through a pipe.
        run(native, "--self-test")
        run(native, "--setup-check", "--no-start", "--codex", str(fake_codex), "--port", str(port))
        failure = json.loads(run(extracted / "Contents/MacOS/tokenmaxxer", "setup", "--json", success=False))
        assert failure["code"] == "move_app"
        extracted.rename(root / "Moved Transfer.app")
        data = profile.data_path(home)
        assert json.loads(run(cli, "--json", "list"))["entries"] == []
        run(cli, "pause")
        assert json.loads(run(cli, "--json", "list"))["paused"] is True
        queue_before = (data / "resume-queue.json").read_bytes()
        settings = json.loads((data / "settings.json").read_text())
        assert settings["codex"] == str(fake_codex) and settings["port"] == port
        agents = home / "Library/LaunchAgents"
        for label in profile.labels:
            config = plistlib.loads((agents / f"{label}.plist").read_bytes())
            assert Path(config["ProgramArguments"][0]).is_file(), config
            assert str(root / "Extracted") not in json.dumps(config), config
            assert config["StandardErrorPath"].startswith(str(data)), config
        worker = plistlib.loads((agents / f"{profile.labels[0]}.plist").read_bytes())
        assert worker["ProgramArguments"] == [str(cli), "--state", str(data / "resume-queue.json"),
                                               "--codex", str(fake_codex), "tick"]
        ui = plistlib.loads((agents / f"{profile.labels[1]}.plist").read_bytes())
        assert ui["ProgramArguments"] == [str(cli), "dashboard", "--state", str(data / "resume-queue.json"),
                                           "--codex", str(fake_codex), "--port", str(port), "serve"]
        menu = plistlib.loads((agents / f"{profile.labels[2]}.plist").read_bytes())
        assert menu["ProgramArguments"] == ["/usr/bin/open", "-g", str(app), "--args", "--background"]
        with tempfile.TemporaryFile() as logs:
            server = subprocess.Popen(ui["ProgramArguments"], cwd=root, env=environment, stdout=logs, stderr=logs)
            try:
                url = f"http://127.0.0.1:{port}"
                for attempt in range(50):
                    if server.poll() is not None:
                        logs.seek(0)
                        raise RuntimeError(logs.read().decode())
                    try:
                        with urllib.request.urlopen(url, timeout=1) as response:
                            assert b"Carrie On" in response.read()
                        break
                    except OSError:
                        if attempt == 49:
                            raise
                        time.sleep(0.1)
                for path, content in (("/app.js", b"fetch"), ("/style.css", b"body"),
                                      ("/app-icon.png", b"\x89PNG")):
                    with urllib.request.urlopen(url + path, timeout=2) as response:
                        assert content in response.read(), path
                request = urllib.request.Request(url + "/api/health", headers={"X-Tokenmaxxer": "1"})
                with urllib.request.urlopen(request, timeout=2) as response:
                    health = json.load(response)
                    assert health == {"version": metadata["version"], "build_id": metadata["build_id"]}
            finally:
                server.terminate()
                server.wait(timeout=10)
        assert (data / "resume-queue.json").read_bytes() == queue_before
        assert app_contents(app) == before, "Setup or runtime modified the signed app"
        other = PROFILES["release" if profile.name == "dev" else "dev"]
        assert not other.data_path(home).exists(), "Setup wrote to the other app's data"
        assert not any((agents / f"{label}.plist").exists() for label in other.labels)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        print("PASS: archive integrity, compatibility, isolated native setup, immutable app, persisted settings, "
              "relocated runtime, queue, service paths, signatures, native self-tests, and dashboard assets")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    verify(parser.parse_args().archive.resolve())

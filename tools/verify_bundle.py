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
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path

from carrie_on.macos.compatibility import audit
from carrie_on.macos.runtime import PROFILES
from tools.branding import check_bundle
from tools.export_reference import check_links


def app_contents(app):
    """Fingerprint files and links to detect any first-launch mutation of the app."""
    return {
        str(path.relative_to(app)): (
            os.readlink(path)
            if path.is_symlink()
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in app.rglob("*")
        if path.is_symlink() or path.is_file()
    }


def verify(archive, profile=None):
    expected = archive.with_suffix(".zip.sha256").read_text().split()[0]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected, "Archive checksum mismatch"
    with zipfile.ZipFile(archive) as bundle:
        if profile is None:
            profiles = [
                candidate
                for candidate in PROFILES.values()
                if f"{candidate.bundle_name}/Contents/Info.plist" in bundle.namelist()
            ]
            assert len(profiles) == 1, "Archive must contain exactly one supported app"
            profile = profiles[0]
        for name in bundle.namelist():
            parts = Path(name).parts
            assert parts[0] in {profile.bundle_name, "__MACOSX"}, name
            assert not {
                ".runtime",
                ".codex",
                "queue.sqlite3",
                "queue.sqlite3-wal",
                "queue.sqlite3-shm",
                "resume-queue.json",
                "resume-queue.lock",
            }.intersection(parts), name
            assert not name.startswith("/") and ".." not in parts, name
    with tempfile.TemporaryDirectory(prefix="carrie_on release check ") as temporary:
        root = Path(temporary).resolve()
        home = root / "Fresh Home"
        home.mkdir()
        subprocess.run(
            ["/usr/bin/ditto", "-x", "-k", str(archive), str(root / "Extracted")],
            check=True,
        )
        extracted = root / "Extracted" / profile.bundle_name
        check_bundle(extracted)
        app = home / "Applications" / profile.bundle_name
        subprocess.run(["/usr/bin/ditto", str(extracted), str(app)], check=True)
        metadata = json.loads((app / "Contents/Resources/release.json").read_text())
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        assert info["CFBundleDisplayName"] == profile.app_name
        assert info["CFBundleIdentifier"] == profile.identifier
        assert info["CarrieOnServiceLabels"] == list(profile.labels)
        assert info["CarrieOnDataDirectory"] == profile.data_directory
        assert metadata["profile"] == profile.name
        distributions = list((app / "Contents/Resources").glob("carrie_on-*.dist-info/METADATA"))
        assert len(distributions) == 1, "Missing packaged Carrie On distribution metadata"
        distribution = Parser().parsestr(distributions[0].read_text())
        assert (
            distribution["Name"] == "carrie-on" and distribution["Version"] == metadata["version"]
        )
        assert (app / "Contents/Resources" / info["CFBundleIconFile"]).read_bytes()[:4] == b"icns"
        assert audit(app, metadata["minimum_macos"]) == metadata["binary_audit"]
        documentation = app / "Contents/Resources/Documentation"
        for name in (
            "README.md",
            "USAGE.md",
            "INSTALL.md",
            "RELEASING.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "LICENSE",
            "features/index.md",
            "assets/README.md",
            "reference/python/index.html",
            "reference/native/index.html",
        ):
            assert (documentation / name).is_file(), f"Missing bundled documentation: {name}"
        check_links(documentation / "reference")
        for document in documentation.rglob("*.md"):
            for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
                if "://" in target or target.startswith(("mailto:", "#")):
                    continue
                linked = target.split("#")[0]
                assert not linked or (document.parent / linked).exists(), (
                    f"Broken bundled link: {document.name}: {target}"
                )
        assert (app / "Contents/Resources/Licenses/Carrie-On.txt").read_text() == (
            documentation / "LICENSE"
        ).read_text()
        before = app_contents(app)
        cli = app / "Contents/MacOS/carrie_on"
        native = app / "Contents/MacOS/CarrieOnMenu"
        fake_codex = root / "fake-codex"
        fake_codex.write_text(
            f"#!{sys.executable}\n"
            + """
import json, sys
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.162.1")
    sys.exit()
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"userAgent":"codex/0.162.1"}
    elif request["method"] == "account/rateLimits/read":
        bucket = {"limitId":"codex", "primary":{"usedPercent":25,"resetsAt":4000000000,"windowDurationMins":300}}
        result = {"accountId":"bundle-fixture","ordinaryUsageAllowed":True,
                  "rateLimits":bucket,"rateLimitsByLimitId":{"codex":bucket}}
    else:
        raise AssertionError("Unexpected fixture method: " + request["method"])
    print(json.dumps({"id":request["id"],"result":result}), flush=True)
"""
        )
        fake_codex.chmod(0o755)
        environment = {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(root),
            "LANG": "en_US.UTF-8",
        }
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        def run(executable, *arguments, success=True):
            process = subprocess.Popen(
                [str(executable), *arguments],
                env=environment,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
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

        assert run(cli, "--version").strip() == f"carrie-on {metadata['version']}"
        # The same native helper used by first launch drives setup through a pipe.
        run(native, "--self-test")
        run(
            native,
            "--setup-check",
            "--no-start",
            "--codex",
            str(fake_codex),
            "--port",
            str(port),
        )
        failure = json.loads(
            run(extracted / "Contents/MacOS/carrie_on", "setup", "--json", success=False)
        )
        assert failure["code"] == "move_app"
        extracted.rename(root / "Moved Transfer.app")
        data = profile.data_path(home)
        settings = json.loads((data / "settings.json").read_text())
        assert settings["codex"] == str(fake_codex) and settings["port"] == port
        agents = home / "Library/LaunchAgents"
        for label in profile.labels:
            config = plistlib.loads((agents / f"{label}.plist").read_bytes())
            assert Path(config["ProgramArguments"][0]).is_file(), config
            assert str(root / "Extracted") not in json.dumps(config), config
            assert config["StandardErrorPath"].startswith(str(data)), config
        backend = plistlib.loads((agents / f"{profile.labels[0]}.plist").read_bytes())
        assert backend["ProgramArguments"] == [
            str(cli),
            "server",
            "--state",
            str(data / "queue.sqlite3"),
            "--codex",
            str(fake_codex),
            "--port",
            str(port),
            "--interval",
            "60",
            "serve",
        ]
        assert backend["KeepAlive"] is True
        assert not (agents / f"{profile.service_prefix}.plist").exists()
        menu = plistlib.loads((agents / f"{profile.labels[1]}.plist").read_bytes())
        assert menu["ProgramArguments"] == [
            "/usr/bin/open",
            "-g",
            str(app),
            "--args",
            "--background",
        ]
        with tempfile.TemporaryFile() as logs:
            server = subprocess.Popen(
                backend["ProgramArguments"],
                cwd=root,
                env=environment,
                stdout=logs,
                stderr=logs,
            )
            try:
                url = f"http://127.0.0.1:{port}"
                for attempt in range(50):
                    if server.poll() is not None:
                        logs.seek(0)
                        raise RuntimeError(logs.read().decode())
                    try:
                        with urllib.request.urlopen(
                            urllib.request.Request(
                                url + "/api/v1/health", headers={"X-Carrie-On": "1"}
                            ),
                            timeout=1,
                        ) as response:
                            assert json.load(response)["version"] == metadata["version"]
                        break
                    except OSError:
                        if attempt == 49:
                            raise
                        time.sleep(0.1)
                try:
                    urllib.request.urlopen(
                        urllib.request.Request(url, headers={"X-Carrie-On": "1"}),
                        timeout=2,
                    )
                except urllib.error.HTTPError as error:
                    assert error.code == 404
                else:
                    raise AssertionError("Browser view is still available")
                request = urllib.request.Request(
                    url + "/api/v1/health", headers={"X-Carrie-On": "1"}
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    health = json.load(response)
                    assert (
                        health["version"] == metadata["version"]
                        and health["build_id"] == metadata["build_id"]
                    )
                    assert health["worker_healthy"], "Packaged dispatch worker is unhealthy"
                assert (
                    json.loads(run(cli, "--server", url, "--json", "queue", "list"))["items"] == []
                )
                usage = json.loads(run(cli, "--server", url, "--json", "usage"))
                assert usage["accountId"] == "bundle-fixture", "Bundled SDK read failed"
                assert "pause" not in run(cli, "queue", "--help")
                assert (data / "queue.sqlite3").is_file()
            finally:
                server.terminate()
                server.wait(timeout=10)
        assert not (data / "resume-queue.json").exists()
        assert app_contents(app) == before, "Setup or runtime modified the signed app"
        other = PROFILES["release" if profile.name == "dev" else "dev"]
        assert not other.data_path(home).exists(), "Setup wrote to the other app's data"
        assert not any((agents / f"{label}.plist").exists() for label in other.labels)
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
            check=True,
        )
        print(
            "PASS: archive integrity, compatibility, isolated native setup, immutable app, persisted settings, "
            "relocated runtime, queue, service paths, signatures, native self-tests, and local API"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    verify(parser.parse_args().archive.resolve())

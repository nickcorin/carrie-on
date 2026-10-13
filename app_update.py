"""Read public GitHub releases and stage verified app updates without changing installation."""

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from app_runtime import APP_BUNDLE_NAME, APP_IDENTIFIER, RUNTIME, VERSION
from macos_compatibility import version_tuple


REPOSITORY = "https://github.com/nickcorin/carrie-on"
API = "https://api.github.com/repos/nickcorin/carrie-on/releases"
MAX_ARCHIVE = 512 * 1024 * 1024


class ReleaseUnavailable(ValueError):
    """A release or asset is not publicly available from GitHub."""


def release_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError("The release has an invalid version.")
    return tuple(map(int, value.split(".")))


def download(url, destination, limit):
    # System curl uses macOS trust roots, including in the self-contained Python bundle.
    result = subprocess.run(["/usr/bin/curl", "--disable", "--fail", "--silent", "--show-error", "--location",
                             "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "15",
                             "--max-time", "180", "--max-filesize", str(limit),
                             "--user-agent", f"Carrie-On/{VERSION}", "--write-out", "%{http_code}",
                             "--output", str(destination), url],
                            capture_output=True, text=True, timeout=190)
    if result.returncode:
        if result.stdout == "404":
            raise ReleaseUnavailable("This release or its update assets are not publicly available on GitHub yet.")
        if result.stdout in ("403", "429"):
            raise ValueError("GitHub declined the update request. Wait a few minutes and try again.")
        raise ValueError("Could not download the update from GitHub. Check your connection and try again. "
                         + result.stderr.strip())
    if destination.stat().st_size > limit:
        raise ValueError("The update download exceeds the supported size.")


def read_json(url):
    with tempfile.TemporaryDirectory(prefix="carrie-release-") as temporary:
        path = Path(temporary) / "metadata.json"
        download(url, path, 1024 * 1024)
        return json.loads(path.read_text())


@dataclass(frozen=True)
class UpdateRelease:
    version: str
    archive: str
    sha256: str
    size: int
    minimum_macos: str

    @property
    def url(self):
        return f"{REPOSITORY}/releases/download/v{self.version}/{self.archive}"

    @classmethod
    def read(cls, release, manifest, current, architecture, macos):
        """Validate release metadata; return None for stable versions already installed."""
        if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
            raise ValueError("Only published stable releases can be installed.")
        tag = release.get("tag_name", "")
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise ValueError("The release tag is invalid.")
        version = tag[1:]
        if release_version(version) <= release_version(current):
            return None
        name = f"Carrie-On-{version}-macos-{architecture}"
        if not isinstance(manifest, dict) or (manifest.get("version"), manifest.get("architecture"), manifest.get("archive")) != (version, architecture, name + ".zip"):
            raise ValueError("The release manifest does not match this Mac or release.")
        digest, size = manifest.get("sha256"), manifest.get("bytes")
        minimum = manifest.get("minimum_macos")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or type(size) is not int or not 0 < size <= MAX_ARCHIVE:
            raise ValueError("The release checksum or download size is invalid.")
        if not isinstance(minimum, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", minimum):
            raise ValueError("The release has an invalid macOS requirement.")
        if version_tuple(macos) < version_tuple(minimum):
            raise ValueError(f"Carrie On {version} requires macOS {minimum} or later.")
        assets = release.get("assets", [])
        for suffix in (".zip", ".json"):
            expected_url = f"{REPOSITORY}/releases/download/{tag}/{name}{suffix}"
            matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name + suffix
                       and asset.get("browser_download_url") == expected_url and asset.get("state") == "uploaded"]
            if len(matches) != 1:
                raise ValueError("This release is missing the required update assets.")
        return cls(version, name + ".zip", digest, size, minimum)


def check_update(version=None):
    """Check latest, or revalidate the exact version the user accepted before download."""
    if version is not None:
        release_version(version)
    try:
        release = read_json(f"{API}/tags/v{version}" if version else f"{API}/latest")
    except ReleaseUnavailable:
        if version is None:
            return None
        raise
    if not isinstance(release, dict):
        raise ValueError("GitHub returned invalid release metadata.")
    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("GitHub returned an invalid release tag.")
    if release_version(tag[1:]) <= release_version(VERSION):
        return None
    if version is not None and tag != f"v{version}":
        raise ValueError("The requested release changed. Check for updates again.")
    architecture = platform.machine()
    name = f"Carrie-On-{tag[1:]}-macos-{architecture}.json"
    manifest = read_json(f"{REPOSITORY}/releases/download/{tag}/{name}")
    return UpdateRelease.read(release, manifest, VERSION, architecture, platform.mac_ver()[0])


def validate_archive(archive, release):
    """Reject corruption, traversal, escaping symlinks, and excessive expanded payloads."""
    if archive.stat().st_size != release.size:
        raise ValueError("The update download is incomplete. Try again.")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != release.sha256:
        raise ValueError("The update checksum does not match. The installed app is unchanged.")
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > 50000 or sum(entry.file_size for entry in entries) > 2 * 1024**3:
            raise ValueError("The expanded update exceeds the supported size.")
        links = {}
        names = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] not in (APP_BUNDLE_NAME, "__MACOSX") or str(path).casefold() in names:
                raise ValueError("The update archive contains an unsafe path.")
            names.add(str(path).casefold())
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                if entry.file_size > 4096:
                    raise ValueError("The update archive contains an invalid link.")
                target = package.read(entry).decode("utf-8")
                resolved = os.path.normpath(str(path.parent / target))
                if not resolved.startswith(path.parts[0] + "/") or target.startswith("/"):
                    raise ValueError("The update archive contains an escaping link.")
                links[str(path).casefold()] = target
            elif stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("The update archive contains an unsupported file type.")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if any(str(parent).casefold() in links for parent in path.parents):
                raise ValueError("The update archive writes through a symbolic link.")
        for name, target in links.items():
            pending = list(PurePosixPath(name).parent.parts) + target.split("/")
            resolved = []
            traversed = 0
            while pending:
                part = pending.pop(0)
                if part in ("", "."):
                    continue
                if part == "..":
                    if len(resolved) <= 1:
                        raise ValueError("The update archive contains an escaping link chain.")
                    resolved.pop()
                    continue
                resolved.append(part.casefold())
                link = links.get("/".join(resolved))
                if link is not None:
                    traversed += 1
                    if traversed > 40:
                        raise ValueError("The update archive contains a symbolic link cycle.")
                    resolved.pop()
                    pending = link.split("/") + pending


def prepare_update(release, app):
    """Return a verified sibling app path, retaining it for native atomic installation.

    The caller owns the returned staging directory. Failures remove staging and leave
    the installed bundle and user data unchanged. No downloaded executable is run.
    """
    stage = Path(tempfile.mkdtemp(prefix=".Carrie-On-update-", dir=app.parent))
    try:
        archive = stage / release.archive
        download(release.url, archive, release.size)
        validate_archive(archive, release)
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(stage)],
                       check=True, capture_output=True, timeout=120)
        candidate = stage / APP_BUNDLE_NAME
        # Recheck resolved links after extraction, including chains of links.
        for path in candidate.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(candidate.resolve()):
                raise ValueError("The extracted update contains an escaping link.")
        info = plistlib.loads((candidate / "Contents/Info.plist").read_bytes())
        metadata = json.loads((candidate / "Contents/Resources/release.json").read_text())
        if (info.get("CFBundleIdentifier") != APP_IDENTIFIER
                or info.get("CFBundleShortVersionString") != release.version
                or info.get("CFBundleExecutable") != "TokenmaxxerMenu"
                or info.get("TokenmaxxerStandalone") is not True
                or metadata.get("version") != release.version
                or metadata.get("architecture") != platform.machine()
                or metadata.get("minimum_macos") != release.minimum_macos):
            raise ValueError("The downloaded app does not match the release.")
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(candidate)],
                       check=True, capture_output=True, timeout=60)
        archive.unlink()
        shutil.rmtree(stage / "__MACOSX", ignore_errors=True)
        return candidate
    except BaseException:
        shutil.rmtree(stage)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "prepare"))
    parser.add_argument("--version", help="Exact release accepted by the user; required for prepare")
    args = parser.parse_args(argv)
    if args.action == "prepare" and not args.version:
        parser.error("prepare requires --version")
    try:
        if not RUNTIME.bundled:
            raise ValueError("Built-in updates are available in the standalone Carrie On.app. Update source installations with Git.")
        release = check_update(args.version)
        result = {"release": asdict(release) if release else None}
        if args.action == "prepare":
            if release is None:
                raise ValueError("This update is no longer newer than the installed app.")
            result["app"] = str(prepare_update(release, RUNTIME.app))
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        print(json.dumps({"message": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

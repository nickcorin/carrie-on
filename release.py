#!/usr/bin/env python3
"""Validate release metadata and print the current release notes to stdout."""

import argparse
import hashlib
import json
import re
import sys
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Release:
    """A numeric release version and its nonempty changelog section."""

    version: str
    notes: str

    @property
    def tag(self):
        return f"v{self.version}"

    @classmethod
    def read(cls, root=ROOT, tag=None):
        """Read VERSION and CHANGELOG.md; reject invalid or mismatched metadata.

        tag, when supplied, must be exactly vMAJOR.MINOR.PATCH for VERSION.
        Raise ValueError before any build or publishing side effect. File errors
        propagate as OSError. No environment variables or Git state are read.
        """
        version = (root / "VERSION").read_text().strip()
        if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
            raise ValueError("VERSION must contain a numeric major.minor.patch release version")
        if tag is not None and tag != f"v{version}":
            raise ValueError(f"Release tag {tag!r} does not match VERSION; expected v{version}")
        changelog = (root / "CHANGELOG.md").read_text()
        sections = re.split(r"^## +(.+?)\s*$", changelog, flags=re.MULTILINE)
        matches = [sections[index + 1].strip() for index in range(1, len(sections), 2)
                   if sections[index] == version]
        if len(matches) != 1 or not matches[0]:
            raise ValueError(f"CHANGELOG.md must contain exactly one nonempty '## {version}' section")
        return cls(version, matches[0])


def source_commit(root=ROOT):
    """Return the commit of a clean Git checkout, or None for dirty/exported source."""
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root, text=True,
            stderr=subprocess.DEVNULL)
        if status:
            return None
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    except subprocess.CalledProcessError:
        return None


def check_assets(directory, release, commit):
    """Reject stale or corrupted Apple Silicon artifacts before uploading them.

    Require the current version, clean source commit, byte count, checksum file,
    and manifest hash to agree. Return the three paths; do not modify artifacts.
    """
    name = f"Carrie-On-{release.version}-macos-arm64"
    archive = directory / f"{name}.zip"
    checksum = directory / f"{name}.zip.sha256"
    manifest = directory / f"{name}.json"
    metadata = json.loads(manifest.read_text())
    if not isinstance(metadata, dict):
        raise ValueError("Invalid release manifest; rebuild with just ci")
    if (not commit or metadata.get("source_commit") != commit
            or metadata.get("version") != release.version
            or metadata.get("architecture") != "arm64"
            or metadata.get("archive") != archive.name):
        raise ValueError("Artifacts do not match this clean commit; run just ci after committing")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if (metadata.get("sha256") != digest or metadata.get("bytes") != archive.stat().st_size
            or checksum.read_text().strip() != f"{digest}  {archive.name}"):
        raise ValueError("Release checksum or size mismatch; rebuild with just ci")
    return archive, checksum, manifest


def check_tag(tag, commit):
    """Require the existing local release tag to identify the checked source commit."""
    tagged = subprocess.check_output(["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"], text=True).strip()
    if tagged != commit:
        raise ValueError("Release tag points to different source; do not move it, choose a new version")


def check_draft(tag):
    """Require an existing draft in the checkout's GitHub repository before mutation."""
    result = subprocess.check_output(["gh", "release", "view", tag, "--json", "isDraft"], text=True)
    if json.loads(result).get("isDraft") is not True:
        raise ValueError("Published releases cannot be changed; create a new version")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog="Example: python3 release.py --tag v0.5.1 > release-notes.md")
    parser.add_argument("--tag", help="Require a Git tag matching VERSION (vMAJOR.MINOR.PATCH)")
    parser.add_argument("--check-source", action="store_true", help="Require a clean, committed Git checkout")
    parser.add_argument("--check-assets", type=Path, metavar="DIRECTORY", help="Verify artifacts against the clean current commit")
    parser.add_argument("--check-draft", action="store_true", help="Require an existing GitHub draft for this version")
    parser.add_argument("--check-tag", action="store_true", help="Require an existing local tag matching the current commit")
    args = parser.parse_args()
    try:
        release = Release.read(tag=args.tag)
        if args.check_source or args.check_assets or args.check_draft or args.check_tag:
            commit = source_commit()
            if commit is None:
                raise ValueError("Commit all source changes before releasing; the checkout must be clean")
            if args.check_assets:
                check_assets(args.check_assets, release, commit)
            if args.check_tag:
                check_tag(release.tag, commit)
            if args.check_draft:
                check_draft(release.tag)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Carrie On release: {error}", file=sys.stderr)
        return 1
    print(release.notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())

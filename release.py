#!/usr/bin/env python3
"""Validate release metadata and print the current release notes to stdout."""

import argparse
import re
import sys
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


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog="Example: python3 release.py --tag v0.5.1 > release-notes.md")
    parser.add_argument("--tag", help="Require a Git tag matching VERSION (vMAJOR.MINOR.PATCH)")
    args = parser.parse_args()
    try:
        release = Release.read(tag=args.tag)
    except (OSError, ValueError) as error:
        print(f"Carrie On release: {error}", file=sys.stderr)
        return 1
    print(release.notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())

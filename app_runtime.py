"""Paths and launch commands shared by source and self-contained installations."""

import sys
from dataclasses import dataclass
from pathlib import Path


VERSION = (Path(__file__).resolve().parent / "VERSION").read_text().strip()
MINIMUM_MACOS = "13.0"
APP_NAME = "Carrie On"
APP_BUNDLE_NAME = f"{APP_NAME}.app"
# Keep the installed identity and data location stable across branding changes.
APP_IDENTIFIER = "com.nickcorin.tokenmaxxer.app"


@dataclass(frozen=True)
class Runtime:
    """Immutable resource, writable-data, and executable locations for a process."""

    resources: Path
    data: Path
    executable: Path
    bundled: bool

    @classmethod
    def current(cls):
        resources = Path(__file__).resolve().parent
        bundled = getattr(sys, "frozen", False)
        data = Path.home() / "Library/Application Support/Tokenmaxxer" if bundled else resources
        return cls(resources, data.resolve(), Path(sys.executable).resolve(), bundled)

    @property
    def logs(self):
        return self.data / ".runtime"

    @property
    def app(self):
        """Return the enclosing app bundle for a frozen backend."""
        return self.executable.parent.parent.parent

    @property
    def state(self):
        return self.data / "resume-queue.json"

    def command(self, component):
        """Return argv for the worker or dashboard, without requiring external Python when bundled."""
        if self.bundled:
            return [str(self.executable)] + (["dashboard"] if component == "dashboard" else [])
        script = "tokenmaxxer_ui.py" if component == "dashboard" else "tokenmaxxer.py"
        return [str(self.executable), str(self.resources / script)]


RUNTIME = Runtime.current()

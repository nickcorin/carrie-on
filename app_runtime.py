"""Paths and launch commands shared by source and self-contained installations."""

import json
import sys
from dataclasses import dataclass
from pathlib import Path


VERSION = (Path(__file__).resolve().parent / "VERSION").read_text().strip()
MINIMUM_MACOS = "13.0"
@dataclass(frozen=True)
class BuildProfile:
    """Installation namespace; application behavior is shared by both builds."""

    name: str
    app_name: str
    service_prefix: str
    data_directory: str
    port: int

    @property
    def identifier(self):
        return self.service_prefix + ".app"

    @property
    def bundle_name(self):
        return self.app_name + ".app"

    @property
    def labels(self):
        return (self.service_prefix, self.service_prefix + ".ui", self.service_prefix + ".menu")

    def data_path(self, home):
        return home / "Library/Application Support" / self.data_directory


# Preserve the released app's existing identity and user data.
PROFILES = {
    "release": BuildProfile("release", "Carrie On", "com.nickcorin.tokenmaxxer", "Tokenmaxxer", 8766),
    "dev": BuildProfile("dev", "Carrie On Dev", "com.nickcorin.tokenmaxxer.dev", "Carrie On Dev", 8767),
}


def read_profile(resources, bundled):
    """Frozen builds select their namespace from signed resources, never shell state."""
    if not bundled:
        return PROFILES["release"]
    name = json.loads((resources / "build-profile.json").read_text())["profile"]
    if name not in PROFILES:
        raise ValueError("Unknown build profile")
    return PROFILES[name]


PROFILE = read_profile(Path(__file__).resolve().parent, getattr(sys, "frozen", False))
APP_NAME = PROFILE.app_name
APP_BUNDLE_NAME = PROFILE.bundle_name
APP_IDENTIFIER = PROFILE.identifier


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
        data = PROFILE.data_path(Path.home()) if bundled else resources
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
        """Return argv for the worker or local API, without requiring external Python when bundled."""
        if self.bundled:
            return [str(self.executable)] + (["dashboard"] if component == "dashboard" else [])
        script = "tokenmaxxer_ui.py" if component == "dashboard" else "tokenmaxxer.py"
        return [str(self.executable), str(self.resources / script)]


RUNTIME = Runtime.current()

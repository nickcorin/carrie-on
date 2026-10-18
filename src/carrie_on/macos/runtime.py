"""Explicit installation namespaces, resource locations, and launch commands."""

import json
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Literal

MINIMUM_MACOS = "13.0"


@dataclass(frozen=True)
class BuildProfile:
    """Installation namespace shared by the backend and its native client."""

    name: str
    app_name: str
    service_prefix: str
    data_directory: str
    port: int

    @property
    def identifier(self) -> str:
        return self.service_prefix + ".app"

    @property
    def bundle_name(self) -> str:
        return self.app_name + ".app"

    @property
    def labels(self) -> tuple[str, str]:
        return (self.service_prefix + ".ui", self.service_prefix + ".menu")

    def data_path(self, home: Path) -> Path:
        return home / "Library/Application Support" / self.data_directory


PROFILES = {
    "release": BuildProfile("release", "Carrie On", "com.nickcorin.carrieon", "Carrie On", 8766),
    "dev": BuildProfile(
        "dev", "Carrie On Dev", "com.nickcorin.carrieon.dev", "Carrie On Dev", 8767
    ),
}


def read_profile(resources: Path, bundled: bool) -> BuildProfile:
    """Read a frozen build's namespace from signed resources, never shell state."""
    if not bundled:
        return PROFILES["release"]
    name = json.loads((resources / "build-profile.json").read_text())["profile"]
    if name not in PROFILES:
        raise ValueError("Unknown build profile")
    return PROFILES[name]


@dataclass(frozen=True)
class Runtime:
    """Resolved process configuration, passed explicitly to platform operations.

    Direct source servers use checkout-local data. Registered services use the
    selected profile's Application Support directory. Executable paths retain
    virtual-environment symlinks because resolving them changes Python's imports.
    """

    resources: Path
    data: Path
    executable: Path
    bundled: bool
    profile: BuildProfile = PROFILES["release"]

    @classmethod
    def current(cls, profile: BuildProfile | None = None, *, registered: bool = False) -> "Runtime":
        """Resolve configuration at an entrypoint; importing this module has no I/O."""
        bundled = bool(getattr(sys, "frozen", False))
        resources = (
            Path(getattr(sys, "_MEIPASS")) if bundled else Path(__file__).resolve().parents[3]
        )
        packaged_profile = read_profile(resources, bundled)
        if bundled and profile is not None and profile != packaged_profile:
            raise ValueError("Use the selected profile's app to install its backend")
        profile = profile or packaged_profile
        if not bundled and not registered and (resources / "pyproject.toml").exists():
            data = resources
        else:
            data = profile.data_path(Path.home())
        return cls(resources, data.resolve(), Path(sys.executable).absolute(), bundled, profile)

    @property
    def version(self) -> str:
        source = self.resources / "VERSION"
        return source.read_text().strip() if source.exists() else version("carrie-on")

    @property
    def logs(self) -> Path:
        return self.data / ".runtime"

    @property
    def app(self) -> Path:
        """Return the enclosing app bundle for a frozen backend."""
        return self.executable.parent.parent.parent

    @property
    def state(self) -> Path:
        return self.data / "queue.sqlite3"

    def command(self, component: Literal["server", "cli"]) -> list[str]:
        """Build an argv usable outside the checkout and without shell activation."""
        if self.bundled:
            return [str(self.executable)] + (["server"] if component == "server" else [])
        module = "carrie_on.server" if component == "server" else "carrie_on"
        return [str(self.executable), "-m", module]

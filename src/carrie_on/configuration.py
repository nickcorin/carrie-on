"""Load connection settings without probing external programs or changing services."""

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class SettingsOverrides:
    """Supply settings for this invocation. None leaves each saved value unchanged."""

    codex: str | None = None
    port: int | None = None
    interval: int | None = None
    socket: Path | None = None
    codex_home: Path | None = None


@dataclass(frozen=True, kw_only=True)
class Settings:
    """Hold the Codex connection and polling settings for an installation."""

    codex: str
    port: int
    interval: int = 60
    socket: str | None = None
    codex_home: str | None = None
    schema_version: int = 1

    @classmethod
    def load(
        cls,
        data: Path,
        overrides: SettingsOverrides = SettingsOverrides(),
        *,
        default_port: int = 8766,
    ) -> "Settings":
        """Read saved settings, apply supplied values, and validate the result.

        Installers call this while holding install.lock. Resolve socket and Codex
        home paths against the current working directory. Saving absolute paths keeps
        later launches pointed at the same files. Invalid settings raise ValueError
        before the installer changes any service definitions.
        """
        path = data / "settings.json"
        if path.exists():
            saved = json.loads(path.read_text())
        else:
            saved = {}
        if not isinstance(saved, dict):
            raise ValueError("settings.json must contain a JSON object")
        if type(saved.get("schema_version", 1)) is not int or saved.get("schema_version", 1) != 1:
            raise ValueError("Settings were created by a newer version of Carrie On")
        codex = overrides.codex if overrides.codex is not None else saved.get("codex")
        port = overrides.port if overrides.port is not None else saved.get("port")
        interval = overrides.interval if overrides.interval is not None else saved.get("interval")
        socket = overrides.socket if overrides.socket is not None else saved.get("socket")
        codex_home = (
            overrides.codex_home if overrides.codex_home is not None else saved.get("codex_home")
        )
        if codex_home is None and "codex_home" not in saved:
            codex_home = os.environ.get("CODEX_HOME")
        if port is None:
            port = default_port
        if interval is None:
            interval = 60
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("Port must be between 1024 and 65535")
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 30:
            raise ValueError("Interval must be at least 30 seconds")
        if codex is not None and (not isinstance(codex, str) or not codex.strip()):
            raise ValueError("codex must be a nonempty executable name or path")
        return cls(
            codex=codex or "codex",
            port=port,
            interval=interval,
            socket=absolute_setting_path("socket", socket),
            codex_home=absolute_setting_path("codex_home", codex_home),
        )

    def environment(self) -> dict[str, str]:
        """Build environment variables for child processes without changing os.environ."""
        environment = {
            "PATH": f"{Path(self.codex).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        }
        if self.codex_home:
            environment["CODEX_HOME"] = self.codex_home
        return environment


def absolute_setting_path(name: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError(f"{name} must be a nonempty path")
    return str(Path(value).expanduser().resolve())

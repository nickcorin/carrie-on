"""Login registration shared with the native lifecycle controller.

Service definitions remain available for manual launch when login is disabled.
"""

import fcntl
import os
import plistlib
import subprocess
import time
from pathlib import Path
from typing import Literal

from carrie_on.macos.runtime import BuildProfile


def directory(profile: BuildProfile) -> Path:
    return profile.data_path(Path.home()) / "Lifecycle"


def launch_at_login(profile: BuildProfile) -> bool:
    path = directory(profile) / "preferences.plist"
    if not path.exists():
        return True
    value = plistlib.loads(path.read_bytes()).get("launch_at_login")
    if type(value) is not bool:
        raise ValueError("Invalid launch-at-login preference")
    return value


def agent_path(label: str, profile: BuildProfile) -> Path:
    if label not in profile.labels:
        raise ValueError("Unknown Carrie On service")
    root = (
        Path.home() / "Library/LaunchAgents"
        if launch_at_login(profile)
        else directory(profile) / "Agents"
    )
    return root / f"{label}.plist"


def write_agent(configuration: dict, profile: BuildProfile) -> Path:
    """Save a definition under the native controller's lock; return its bootstrap path."""
    label = configuration["Label"]
    if label not in profile.labels:
        raise ValueError("Unknown Carrie On service")
    root = directory(profile)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = root / "Agents" / f"{label}.plist"
        login = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        enabled = launch_at_login(profile)
        payload = plistlib.dumps(configuration)
        for path in (saved, login) if enabled else (saved,):
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = path.with_suffix(".plist.tmp")
            temporary.write_bytes(payload)
            temporary.chmod(0o600)
            os.replace(temporary, path)
        if not enabled:
            login.unlink(missing_ok=True)
        return login if enabled else saved


def remove_agent(label: str, profile: BuildProfile) -> None:
    """Remove both login and manual-launch definitions, preserving the preference."""
    if label not in profile.labels:
        raise ValueError("Unknown Carrie On service")
    root = directory(profile)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (Path.home() / "Library/LaunchAgents" / f"{label}.plist").unlink(missing_ok=True)
        (root / "Agents" / f"{label}.plist").unlink(missing_ok=True)


def service_loaded(label: str) -> bool:
    return (
        subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True
        ).returncode
        == 0
    )


def unregister_services(labels: tuple[str, ...]) -> None:
    """Unload launchd registrations before replacing or removing their definitions.

    This is an installation operation. Normal service shutdown uses HTTP.
    Absent registrations are harmless; a service that remains loaded is an error.
    """
    for label in reversed(labels):
        if not service_loaded(label):
            continue
        subprocess.run(
            ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            check=True,
            capture_output=True,
        )
        for _ in range(100):
            if not service_loaded(label):
                break
            time.sleep(0.1)
        else:
            raise ValueError(f"The service {label} has not unloaded. Retry setup.")


def control_service(action: Literal["status", "start", "uninstall"], profile: BuildProfile) -> dict:
    """Control only the selected backend, preserving queue data and login policy.

    kickstart starts a stopped, loaded service without restarting a running one.
    Uninstall waits for launchd to release it before removing its definition.
    """
    label = profile.labels[0]
    target = f"gui/{os.getuid()}/{label}"
    loaded = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True)
    if action == "status":
        return {
            "running": loaded.returncode == 0 and "state = running" in loaded.stdout,
            "detail": loaded.stdout or loaded.stderr,
        }
    if action == "start" and loaded.returncode:
        subprocess.run(
            [
                "/bin/launchctl",
                "bootstrap",
                f"gui/{os.getuid()}",
                str(agent_path(label, profile)),
            ],
            check=True,
        )
    elif action == "start":
        subprocess.run(["/bin/launchctl", "kickstart", target], check=True)
    elif action == "uninstall":
        unregister_services((label,))
        remove_agent(label, profile)
    return {"service": action, "data_preserved": True}

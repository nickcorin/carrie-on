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
    if label not in (*profile.labels, profile.service_prefix):
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
    if label not in (*profile.labels, profile.service_prefix):
        raise ValueError("Unknown Carrie On service")
    root = directory(profile)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (Path.home() / "Library/LaunchAgents" / f"{label}.plist").unlink(missing_ok=True)
        (root / "Agents" / f"{label}.plist").unlink(missing_ok=True)


def retire_worker(*, profile: BuildProfile, remove_definition: bool = True) -> None:
    """Stop the retired JSON writer and remove both of its startup definitions."""
    label = profile.service_prefix
    target = f"gui/{os.getuid()}/{label}"
    if subprocess.run(["/bin/launchctl", "print", target], capture_output=True).returncode:
        if remove_definition:
            remove_agent(label, profile)
        return
    subprocess.run(["/bin/launchctl", "bootout", target], capture_output=True)
    for _ in range(100):
        if subprocess.run(["/bin/launchctl", "print", target], capture_output=True).returncode:
            if remove_definition:
                remove_agent(label, profile)
            return
        time.sleep(0.1)
    raise ValueError("The previous worker has not stopped; retry setup before importing its queue")


def control_service(
    action: Literal["status", "start", "stop", "uninstall"], profile: BuildProfile
) -> dict:
    """Control only the selected backend, preserving queue data and login policy.

    Repeated starts, stops, and removal are safe. Stop waits for launchd to release
    the service; failure leaves its definition intact so the caller can retry.
    """
    label = profile.labels[0]
    target = f"gui/{os.getuid()}/{label}"
    loaded = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True)
    if action == "status":
        return {
            "running": loaded.returncode == 0,
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
    elif action in {"stop", "uninstall"}:
        if loaded.returncode == 0:
            subprocess.run(["/bin/launchctl", "bootout", target], check=True)
            for _ in range(100):
                if subprocess.run(
                    ["/bin/launchctl", "print", target], capture_output=True
                ).returncode:
                    break
                time.sleep(0.1)
            else:
                raise ValueError("Backend has not stopped; retry after its active request finishes")
        if action == "uninstall":
            retire_worker(profile=profile)
            remove_agent(label, profile)
    return {"service": action, "data_preserved": True}

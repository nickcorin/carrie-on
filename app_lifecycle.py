"""Login registration shared with the native lifecycle controller.

Service definitions remain available for manual launch when login is disabled.
"""

import fcntl
import os
import plistlib
from pathlib import Path


LABELS = ("com.nickcorin.tokenmaxxer", "com.nickcorin.tokenmaxxer.ui", "com.nickcorin.tokenmaxxer.menu")


def directory():
    return Path.home() / "Library/Application Support/Tokenmaxxer/Lifecycle"


def launch_at_login():
    path = directory() / "preferences.plist"
    if not path.exists():
        return True
    value = plistlib.loads(path.read_bytes()).get("launch_at_login")
    if type(value) is not bool:
        raise ValueError("Invalid launch-at-login preference")
    return value


def agent_path(label):
    if label not in LABELS:
        raise ValueError("Unknown Carrie On service")
    root = Path.home() / "Library/LaunchAgents" if launch_at_login() else directory() / "Agents"
    return root / f"{label}.plist"


def write_agent(configuration):
    """Save a definition under the native controller's lock; return its bootstrap path."""
    label = configuration["Label"]
    if label not in LABELS:
        raise ValueError("Unknown Carrie On service")
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = root / "Agents" / f"{label}.plist"
        login = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        enabled = launch_at_login()
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


def remove_agent(label):
    """Remove both login and manual-launch definitions, preserving the preference."""
    if label not in LABELS:
        raise ValueError("Unknown Carrie On service")
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (Path.home() / "Library/LaunchAgents" / f"{label}.plist").unlink(missing_ok=True)
        (root / "Agents" / f"{label}.plist").unlink(missing_ok=True)

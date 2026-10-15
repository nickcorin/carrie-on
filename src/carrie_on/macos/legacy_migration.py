"""Move data from the previous packaged app during release setup.

Save source copies and the migration journal in the current data directory.
Setup retries skip entries whose UUIDs already exist in storage. Keep the source
copies for recovery, including malformed records and entries replaced during import.
"""

import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from carrie_on.legacy import LegacyImporter, LegacyQueue
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance

PREVIOUS_IDENTIFIER = "com.nickcorin.tokenmaxxer.app"
PREVIOUS_LABELS = (
    "com.nickcorin.tokenmaxxer",
    "com.nickcorin.tokenmaxxer.ui",
    "com.nickcorin.tokenmaxxer.menu",
)
PREVIOUS_DIRECTORY = "Tokenmaxxer"
DATA_FILES = ("settings.json", "resume-queue.json", "Lifecycle/preferences.plist")


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, payload: bytes) -> None:
    """Write and sync a private temporary file, then replace the destination and sync its directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()


@dataclass(frozen=True)
class LegacyMigration:
    """Import the previous installation and keep copies for recovery.

    The caller must hold the current installation lock and quit the previous native
    app. When migration is pending, prepare holds the current JSON queue lock.
    If the previous data directory exists, it also holds that installation's
    installation, lifecycle, and queue locks. These locks last until the context
    exits. Call complete only after the new API passes its health check.
    """

    home: Path
    data: Path
    current_labels: tuple[str, ...]

    @property
    def previous(self) -> Path:
        return self.home / "Library/Application Support" / PREVIOUS_DIRECTORY

    @property
    def recovery(self) -> Path:
        return self.data / "Migration/previous-installation"

    @property
    def journal(self) -> Path:
        return self.recovery / "journal.json"

    @property
    def pending(self) -> bool:
        if self.journal.exists():
            return json.loads(self.journal.read_text())["state"] != "complete"
        if self.previous.exists():
            return True
        # Source installations used the same labels. Import only packaged installations.
        for label in PREVIOUS_LABELS:
            path = self.login_agent(label)
            if path.exists():
                try:
                    arguments = plistlib.loads(path.read_bytes()).get("ProgramArguments", [])
                    if any(
                        isinstance(value, str)
                        and (value.endswith((".app/Contents/MacOS/tokenmaxxer", "/Carrie On.app")))
                        for value in arguments
                    ):
                        return True
                except (ValueError, AttributeError, plistlib.InvalidFileException):
                    continue
        return False

    def login_agent(self, label: str) -> Path:
        return self.home / "Library/LaunchAgents" / f"{label}.plist"

    @contextmanager
    def prepare(self) -> Iterator[None]:
        if not self.pending:
            yield
            return
        if self.previous.is_symlink() or self.recovery.is_symlink():
            raise ValueError("Migration directories must not be symbolic links")
        # Stop workers before taking locks because a worker may hold its queue lock during a send.
        self.stop_services(PREVIOUS_LABELS + self.current_labels[:1])
        with ExitStack() as locks:
            paths = [self.data / "resume-queue.lock"]
            if self.previous.exists():
                paths += [
                    self.previous / name
                    for name in ("install.lock", "Lifecycle/lock", "resume-queue.lock")
                ]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                stream = locks.enter_context(path.open("a"))
                os.chmod(path, 0o600)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise ValueError(
                                "The previous installation is still busy. Reopen Carrie On to retry migration."
                            )
                        time.sleep(0.1)
            if not self.journal.exists():
                self.snapshot()
            journal = json.loads(self.journal.read_text())
            if journal["state"] == "prepared":
                for name in DATA_FILES:
                    if name == "resume-queue.json":
                        continue
                    source = self.recovery / "import" / name
                    if source.exists():
                        atomic_write(self.data / name, source.read_bytes())
                journal["state"] = "applied"
                atomic_write(self.journal, json_bytes(journal))
            database = self.data / "queue.sqlite3"
            SqliteStorageMaintenance(database).initialize()
            importer = LegacyImporter(SqliteQueueStorage(database))
            queue_snapshot = self.recovery / "import/resume-queue.json"
            if queue_snapshot.exists():
                # An earlier installer may have saved the merged queue here.
                importer.import_file(queue_snapshot)
            else:
                queues = [
                    LegacyQueue.read(self.recovery / name / "resume-queue.json")
                    if name in journal.get("queues", [])
                    else LegacyQueue()
                    for name in ("previous", "current")
                ]
                records = queues[0].merge(queues[1], journal.get("prepared_at", time.time()))
                importer.import_records(records)
            yield

    def stop_services(self, labels: tuple[str, ...]) -> None:
        for label in reversed(labels):
            target = f"gui/{os.getuid()}/{label}"
            subprocess.run(
                ["/bin/launchctl", "bootout", target],
                capture_output=True,
                timeout=15,
                check=False,
            )
            for _ in range(100):
                result = subprocess.run(
                    ["/bin/launchctl", "print", target],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode:
                    break
                time.sleep(0.1)
            else:
                raise ValueError(
                    f"The previous service {label} has not stopped. Reopen Carrie On to retry."
                )

    def snapshot(self) -> None:
        self.recovery.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(prefix=".migration-", dir=self.recovery.parent))
        try:
            if self.previous.exists():
                shutil.copytree(self.previous, temporary / "previous", symlinks=True)
            for name in DATA_FILES:
                source = self.data / name
                if source.exists():
                    atomic_write(temporary / "current" / name, source.read_bytes())
            database = self.data / "queue.sqlite3"
            if database.exists():
                backup = temporary / "current/queue.sqlite3"
                backup.parent.mkdir(parents=True, exist_ok=True)
                SqliteStorageMaintenance(database).create_backup(backup)
            agents = temporary / "agents"
            for label in PREVIOUS_LABELS:
                source = self.login_agent(label)
                if source.exists():
                    atomic_write(agents / source.name, source.read_bytes())
            warnings, queues = self.plan(temporary)
            atomic_write(
                temporary / "journal.json",
                json_bytes(
                    {
                        "state": "prepared",
                        "warnings": warnings,
                        "queues": queues,
                        "prepared_at": time.time(),
                    }
                ),
            )
            os.rename(temporary, self.recovery)
            sync_directory(self.recovery.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def plan(self, snapshot: Path) -> tuple[list[str], list[str]]:
        warnings: list[str] = []
        previous, current = snapshot / "previous", snapshot / "current"
        old_settings = self.read_settings(previous / "settings.json", warnings)
        settings = self.read_settings(current / "settings.json", warnings)
        if old_settings or settings:
            atomic_write(
                snapshot / "import/settings.json",
                json_bytes({**old_settings, **settings}),
            )

        preference = "Lifecycle/preferences.plist"
        for root in (current, previous):
            path = root / preference
            if not path.exists():
                continue
            try:
                if path.is_symlink():
                    raise ValueError("symbolic link")
                value = plistlib.loads(path.read_bytes())
                if type(value.get("launch_at_login")) is not bool:
                    raise ValueError("invalid startup preference")
                atomic_write(snapshot / "import" / preference, plistlib.dumps(value))
                break
            except (
                ValueError,
                TypeError,
                AttributeError,
                plistlib.InvalidFileException,
            ) as error:
                warnings.append(f"Preserved unreadable {root.name}/{preference}: {error}")

        queues = []
        for root in (previous, current):
            path = root / "resume-queue.json"
            try:
                if path.is_symlink():
                    raise ValueError("symbolic link")
                LegacyQueue.read(path)
                queues.append(root.name)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                warnings.append(f"Preserved unreadable {root.name}/resume-queue.json: {error}")
        return warnings, queues

    @staticmethod
    def read_settings(path: Path, warnings: list[str]) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            if path.is_symlink():
                raise ValueError("symbolic link")
            value = json.loads(path.read_text())
            if not isinstance(value, dict) or value.get("schema_version", 1) != 1:
                raise ValueError("unsupported settings format")
            for name in ("port", "interval", "codex", "socket", "codex_home"):
                if name not in value:
                    continue
                setting = value[name]
                if name == "port":
                    valid = type(setting) is int and 1024 <= setting <= 65535
                elif name == "interval":
                    valid = type(setting) is int and setting >= 30
                elif name == "codex":
                    valid = isinstance(setting, str) and bool(shutil.which(setting))
                else:
                    valid = setting is None or isinstance(setting, str) and bool(setting.strip())
                if not valid:
                    del value[name]
                    warnings.append(
                        f"Ignored unavailable or invalid {path.parent.name} setting: {name}"
                    )
            return value
        except (ValueError, TypeError) as error:
            warnings.append(f"Preserved unreadable {path.parent.name}/settings.json: {error}")
            return {}

    def complete(self) -> None:
        """Remove old service definitions and move old files to recovery after setup succeeds."""
        if not self.journal.exists() or not self.pending:
            return
        for label in PREVIOUS_LABELS:
            self.login_agent(label).unlink(missing_ok=True)
        agents = self.home / "Library/LaunchAgents"
        if agents.exists():
            sync_directory(agents)
        if self.previous.exists():
            self.previous.rename(self.recovery / "retired")
            sync_directory(self.previous.parent)
            sync_directory(self.recovery)
        journal = json.loads(self.journal.read_text())
        journal["state"] = "complete"
        atomic_write(self.journal, json_bytes(journal))

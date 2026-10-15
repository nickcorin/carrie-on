"""Private SQLite connections and maintenance operations."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from carrie_on.ports import StorageUnavailable
from carrie_on.storage.schema import migrate


class Database:
    """Share a path, never a connection, across storage services and worker threads."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except sqlite3.Error as error:
            raise StorageUnavailable("Could not complete the storage operation") from error


class SqliteStorageMaintenance:
    """Initialize and back up storage without exposing SQLite resources."""

    def __init__(self, path: Path):
        self._database = Database(path)

    def initialize(self) -> None:
        path = self._database.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
            migrate(path)
        except OSError as error:
            raise StorageUnavailable("Could not prepare the queue database") from error

    def create_backup(self, destination: Path) -> None:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.touch(mode=0o600, exist_ok=True)
            destination.chmod(0o600)
            with (
                self._database.connection() as source,
                closing(sqlite3.connect(destination)) as target,
            ):
                source.backup(target)
        except (OSError, sqlite3.Error) as error:
            raise StorageUnavailable("Could not back up the queue database") from error

"""Apply SQL migrations for this release and reject unsupported database versions."""

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from yoyo import get_backend, read_migrations
from yoyo.exceptions import BadMigration, LockTimeout

from carrie_on.ports import StorageUnavailable

SCHEMA_VERSION = 4


def migrate(path: Path) -> None:
    """Apply pending migrations while holding Yoyo's lock against other migration processes."""
    try:
        with closing(sqlite3.connect(path)) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
                raise StorageUnavailable("Database belongs to a newer Carrie On release")
            connection.execute("PRAGMA journal_mode=WAL")
        migrations = read_migrations(str(Path(__file__).with_name("migrations")))
        backend = get_backend("sqlite:///" + quote(str(path.absolute()), safe="/"))
        try:
            backend.connection.execute("PRAGMA foreign_keys=ON")
            with backend.lock():
                if set(backend.get_applied_migration_hashes()) - {
                    migration.hash for migration in migrations
                }:
                    raise StorageUnavailable("Database has migrations unknown to this release")
                backend.apply_migrations(backend.to_apply(migrations))
                if (
                    backend.connection.execute("PRAGMA user_version").fetchone()[0]
                    != SCHEMA_VERSION
                ):
                    raise StorageUnavailable("Database schema does not match this release")
                if backend.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise StorageUnavailable("Database contains invalid references")
        finally:
            backend.connection.close()
    except (sqlite3.Error, BadMigration, LockTimeout) as error:
        raise StorageUnavailable("Could not migrate the queue database") from error

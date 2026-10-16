"""Apply SQL migrations for this release and reject unsupported database versions."""

import sqlite3
from pathlib import Path
from urllib.parse import quote

from yoyo import get_backend, read_migrations
from yoyo.exceptions import BadMigration, LockTimeout

from carrie_on.ports import StorageUnavailable


def migrate(path: Path) -> None:
    """Apply pending migrations while holding Yoyo's lock against other migration processes."""
    try:
        migrations = read_migrations(str(Path(__file__).with_name("migrations")))
        with get_backend("sqlite:///" + quote(str(path.absolute()), safe="/")) as backend:
            backend.connection.execute("PRAGMA foreign_keys=ON")
            with backend.lock():
                if set(backend.get_applied_migration_hashes()) - {
                    migration.hash for migration in migrations
                }:
                    raise StorageUnavailable("Database has migrations unknown to this release")
                backend.apply_migrations(backend.to_apply(migrations))
    except (sqlite3.Error, BadMigration, LockTimeout) as error:
        raise StorageUnavailable("Could not migrate the queue database") from error

"""Executable dependency boundaries for the domain, consumers, and storage adapter."""

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src/carrie_on"


class ApplicationBoundaries(unittest.TestCase):
    def test_inner_components_import_protocols_instead_of_storage_implementation(self):
        allowed = {
            "queue.py": {"codex.models"},
            "ports.py": {"queue"},
            "pagination.py": {"queue"},
            "codex/models.py": set(),
            "codex/client.py": {"codex.models", "codex.sdk"},
            "codex/sdk.py": {"codex.models", "codex.client"},
            "storage/__init__.py": {"storage.database", "storage.queue"},
            "storage/database.py": {"ports", "storage.schema"},
            "storage/schema.py": {"ports"},
            "storage/queue.py": {"queue", "pagination", "storage.database"},
            "legacy.py": {"queue", "ports"},
            "application.py": {"queue", "ports", "pagination", "codex.client", "codex.models"},
            "dispatch.py": {"queue", "ports", "application", "codex.client", "codex.models"},
            "api.py": {"queue", "ports", "application", "dispatch", "codex.models"},
        }
        frameworks = {"fastapi", "typer", "uvicorn", "pydantic"}
        for filename, dependencies in allowed.items():
            for name in self.imports(PACKAGE / filename):
                with self.subTest(module=filename, dependency=name):
                    if name.startswith("carrie_on."):
                        self.assertIn(name.removeprefix("carrie_on."), dependencies)
                    if filename not in {"api.py", "codex/sdk.py"}:
                        self.assertNotIn(name.split(".")[0], frameworks)

    def test_database_libraries_and_transactions_are_private_to_storage(self):
        for path in PACKAGE.rglob("*.py"):
            if "storage" in path.relative_to(PACKAGE).parts:
                continue
            for name in self.imports(path):
                with self.subTest(module=path.name, dependency=name):
                    self.assertNotIn(name.split(".")[0], {"sqlite3", "yoyo", "sqlalchemy"})

    @staticmethod
    def imports(path):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                yield node.module or ""
            elif isinstance(node, ast.Import):
                yield from (alias.name for alias in node.names)

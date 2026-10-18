"""Executable dependency boundaries for the domain, consumers, and storage adapter."""

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src/carrie_on"


class ApplicationBoundaries(unittest.TestCase):
    def test_inner_components_import_protocols_instead_of_storage_implementation(self):
        allowed = {
            "queue.py": {"conversation", "usage"},
            "conversation.py": set(),
            "usage.py": set(),
            "configuration.py": set(),
            "ports.py": {"queue", "conversation", "usage"},
            "pagination.py": {"queue"},
            "codex/parsing.py": {"ports", "usage"},
            "codex/client.py": {"codex.parsing", "codex.sdk", "ports"},
            "codex/sdk.py": {"codex.parsing", "conversation", "ports", "usage"},
            "codex/adapter.py": {"codex.client", "conversation", "ports", "usage"},
            "storage/__init__.py": {"storage.database", "storage.queue"},
            "storage/database.py": {"ports", "storage.schema"},
            "storage/schema.py": {"ports"},
            "storage/queue.py": {"queue", "pagination", "storage.database"},
            "application.py": {"queue", "ports", "pagination", "conversation", "usage"},
            "dispatch.py": {"queue", "ports", "application"},
            "api.py": {"queue", "ports", "application", "dispatch", "usage"},
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
                if node.module == "carrie_on":
                    yield from ("carrie_on." + alias.name for alias in node.names)
                else:
                    yield node.module or ""
            elif isinstance(node, ast.Import):
                yield from (alias.name for alias in node.names)

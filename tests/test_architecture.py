"""Dependency constraints for reusable application components."""

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src/carrie_on"


class ApplicationBoundaries(unittest.TestCase):
    def test_inner_components_cannot_import_frameworks_or_platform_configuration(self):
        allowed = {
            "queue.py": {"codex.models"},
            "codex/models.py": set(),
            "codex/client.py": {"codex.models"},
            "storage.py": {"queue", "legacy"},
            "legacy.py": {"queue"},
            "application.py": {"queue", "storage", "codex.client", "codex.models"},
            "dispatch.py": {"queue", "storage", "application", "codex.client", "codex.models"},
            "api.py": {"queue", "application", "dispatch", "codex.models"},
        }
        frameworks = {"fastapi", "typer", "uvicorn", "pydantic"}
        for filename, dependencies in allowed.items():
            tree = ast.parse((PACKAGE / filename).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imports = [node.module or ""]
                elif isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                else:
                    continue
                for name in imports:
                    with self.subTest(module=filename, dependency=name):
                        if name.startswith("carrie_on."):
                            self.assertIn(name.removeprefix("carrie_on."), dependencies)
                        if filename != "api.py":
                            self.assertNotIn(name.split(".")[0], frameworks)

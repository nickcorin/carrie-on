import tempfile
import unittest
from pathlib import Path

from tools.branding import RETIRED_NAME, check_branding, check_source


class BrandingTests(unittest.TestCase):
    def test_rejects_paths_and_case_insensitive_payloads(self):
        with self.assertRaises(ValueError):
            check_branding(RETIRED_NAME.decode().title() + ".app")
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding), self.assertRaises(ValueError):
                check_branding("Info.plist", RETIRED_NAME.decode().upper().encode(encoding))
        check_branding("Carrie On.app", b"com.nickcorin.carrieon")

    def test_current_source_has_no_retired_branding(self):
        check_source(Path(__file__).resolve().parents[1])

    def test_checks_nonignored_untracked_inputs(self):
        import subprocess

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "release-notes.md").write_bytes(RETIRED_NAME)
            with self.assertRaises(ValueError):
                check_source(root)

    def test_migration_sources_have_no_branding_exemption(self):
        import subprocess

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            migration = root / "src/carrie_on/macos/legacy_migration.py"
            migration.parent.mkdir(parents=True)
            migration.write_bytes(RETIRED_NAME)
            with self.assertRaisesRegex(ValueError, "legacy_migration.py"):
                check_source(root)

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from release import Release


class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "VERSION").write_text("1.2.3\n")
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 1.2.3\n\n- Fix startup.\n\n### Compatibility\n\nmacOS 13+.\n\n"
            "## 1.2.2\n\n- Older change.\n")

    def test_notes_include_only_selected_release_and_keep_subheadings(self):
        release = Release.read(self.root, "v1.2.3")
        self.assertEqual(release.version, "1.2.3")
        self.assertEqual(release.tag, "v1.2.3")
        self.assertEqual(release.notes, "- Fix startup.\n\n### Compatibility\n\nmacOS 13+.")

    def test_mismatched_tag_is_rejected(self):
        for tag in ("v1.2.2", "1.2.3", "v1.2.3-rc1"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "does not match VERSION"):
                Release.read(self.root, tag)

    def test_invalid_versions_are_rejected(self):
        for version in ("", "1.2", "v1.2.3", "01.2.3", "1.2.3/other", "1.2.3\n1.2.4"):
            (self.root / "VERSION").write_text(version)
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "numeric"):
                Release.read(self.root)

    def test_missing_empty_and_duplicate_notes_are_rejected(self):
        for changelog in ("# Changelog\n", "## 1.2.3\n\n## 1.2.2\nOld",
                          "## 1.2.3\nFirst\n## 1.2.3\nSecond"):
            (self.root / "CHANGELOG.md").write_text(changelog)
            with self.subTest(changelog=changelog), self.assertRaisesRegex(ValueError, "nonempty"):
                Release.read(self.root)

    def test_cli_reports_invalid_tag_on_stderr_without_notes(self):
        command = [sys.executable, str(Path(__file__).with_name("release.py")), "--tag", "invalid"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("does not match VERSION", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()

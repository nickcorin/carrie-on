import subprocess
import hashlib
import json
from unittest.mock import patch
import sys
import tempfile
import unittest
from pathlib import Path

from release import Release, check_assets, check_draft, check_tag, source_commit


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


class ReleaseArtifactTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.release = Release("1.2.3", "- Fix startup.")
        self.archive = self.root / "Carrie-On-1.2.3-macos-arm64.zip"
        self.archive.write_bytes(b"archive fixture")
        self.digest = hashlib.sha256(b"archive fixture").hexdigest()
        self.checksum = self.archive.with_suffix(".zip.sha256")
        self.checksum.write_text(f"{self.digest}  {self.archive.name}\n")
        self.manifest = self.archive.with_suffix(".json")
        self.metadata = {"version": "1.2.3", "source_commit": "abc123", "architecture": "arm64",
                         "archive": self.archive.name, "bytes": 15, "sha256": self.digest}
        self.manifest.write_text(json.dumps(self.metadata))

    def test_matching_artifacts_are_returned_without_modification(self):
        paths = check_assets(self.root, self.release, "abc123")
        self.assertEqual([path.name for path in paths], ["Carrie-On-1.2.3-macos-arm64.zip",
                         "Carrie-On-1.2.3-macos-arm64.zip.sha256", "Carrie-On-1.2.3-macos-arm64.json"])
        self.assertEqual(self.archive.read_bytes(), b"archive fixture")

    def test_stale_source_and_uncommitted_builds_cannot_be_released(self):
        for commit in ("other", None):
            with self.subTest(commit=commit), self.assertRaisesRegex(ValueError, "clean commit"):
                check_assets(self.root, self.release, commit)

    def test_corrupted_archive_is_rejected(self):
        self.archive.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "checksum or size"):
            check_assets(self.root, self.release, "abc123")

    def test_changed_checksum_and_manifest_size_are_rejected(self):
        self.checksum.write_text("wrong checksum")
        with self.assertRaisesRegex(ValueError, "checksum or size"):
            check_assets(self.root, self.release, "abc123")
        self.checksum.write_text(f"{self.digest}  {self.archive.name}\n")
        self.metadata["bytes"] = 999
        self.manifest.write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "checksum or size"):
            check_assets(self.root, self.release, "abc123")

    def test_only_clean_source_receives_commit_provenance(self):
        with patch("release.subprocess.check_output", side_effect=["", "abc123\n"]):
            self.assertEqual(source_commit(self.root), "abc123")
        with patch("release.subprocess.check_output", return_value=" M file.py\n"):
            self.assertIsNone(source_commit(self.root))

    def test_published_release_and_wrong_tag_are_rejected(self):
        with patch("release.subprocess.check_output", return_value='{"isDraft": false}'):
            with self.assertRaisesRegex(ValueError, "Published releases"):
                check_draft("v1.2.3")
        with patch("release.subprocess.check_output", return_value="other\n"):
            with self.assertRaisesRegex(ValueError, "different source"):
                check_tag("v1.2.3", "abc123")


if __name__ == "__main__":
    unittest.main()

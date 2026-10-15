"""Generate portable reference pages and reject broken navigation."""

import tempfile
import unittest
from pathlib import Path

from tools.export_reference import check_links, export


class ReferenceTests(unittest.TestCase):
    def test_python_export_includes_packages_and_current_contracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            export(output)
            for name in ("carrie_on", "carrie_on.storage", "carrie_on.codex", "carrie_on.macos"):
                self.assertTrue((output / f"{name}.html").is_file())
            sdk = (output / "carrie_on.codex.sdk.html").read_text()
            self.assertIn("get_chat", sdk)
            self.assertIn("before_open", sdk)
            self.assertNotIn("receipt_id", sdk)
            self.assertNotIn('href="file:', sdk)
            check_links(output)

    def test_link_check_rejects_missing_page_and_fragment(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            index = output / "index.html"
            index.write_text('<a href="missing.html">Missing page</a>')
            with self.assertRaisesRegex(ValueError, "Broken reference link"):
                check_links(output)
            index.write_text('<a href="#missing">Missing fragment</a>')
            with self.assertRaisesRegex(ValueError, "Missing reference anchor"):
                check_links(output)
            index.write_text('<a href="#present">Existing fragment</a><h2 id="present">Title</h2>')
            check_links(output)

"""Exercise the native picker with deterministic, delayed HTTP responses."""

import platform
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(platform.system() == "Darwin", "The chat picker requires macOS")
class NativeChatPagination(unittest.TestCase):
    def test_paging_retry_and_search_races(self):
        with tempfile.TemporaryDirectory(prefix="carrie-pagination-") as temporary:
            build = Path(temporary)
            # Keep the app's shared declarations, replacing only its startup entry point.
            declarations = (ROOT / "menubar/main.swift").read_text().split(
                'if CommandLine.arguments.contains("--self-test") {', 1)[0]
            main = build / "main.swift"
            main.write_text(declarations + (ROOT / "tests/ChatPagination.swift").read_text())
            executable = build / "ChatPaginationTests"
            sources = [ROOT / "menubar" / name for name in
                       ("ChatPicker.swift", "AppSetup.swift", "AppLifecycle.swift", "Brand.swift")]
            compile_result = subprocess.run(
                ["/usr/bin/swiftc", "-module-cache-path", str(build / "cache"),
                 "-o", str(executable), *map(str, sources), str(main)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Chat pagination checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()

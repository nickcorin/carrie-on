"""Entry point for the bundled CLI, dashboard, and per-user installer."""

import sys

import bundle_install
import tokenmaxxer
import tokenmaxxer_ui


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command in {"setup", "remove-installation"}:
        sys.exit(bundle_install.main(sys.argv[1:]))
    if command == "dashboard":
        del sys.argv[1]
        sys.exit(tokenmaxxer_ui.main())
    sys.exit(tokenmaxxer.main())

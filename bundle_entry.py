"""Entry point for the bundled CLI, local API, and per-user installer."""

import sys

import bundle_install
import app_update
import carrie_on
import carrie_on_ui


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "update":
        sys.exit(app_update.main(sys.argv[2:]))
    if command in {"setup", "remove-installation"}:
        sys.exit(bundle_install.main(sys.argv[1:]))
    if command == "dashboard":
        del sys.argv[1]
        sys.exit(carrie_on_ui.main())
    sys.exit(carrie_on.main())

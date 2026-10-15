"""Route commands for the macOS bundle's CLI, server, installer, and updater."""

import sys

from carrie_on import cli, server
from carrie_on.macos import installation, updates

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "update":
        sys.exit(updates.main(sys.argv[2:]))
    if command in {"setup", "remove-installation"}:
        sys.exit(installation.main(sys.argv[1:]))
    if command == "server":
        del sys.argv[1]
        sys.exit(server.main())
    sys.exit(cli.main())

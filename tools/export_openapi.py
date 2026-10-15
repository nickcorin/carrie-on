"""Export the server-owned contract without starting services or touching queue data."""

import argparse
import json
from pathlib import Path

from carrie_on.codex.client import CodexConnections
from carrie_on.server import create_application
from carrie_on.storage import QueueStore


def contract():
    app = create_application(
        QueueStore(Path("unused.sqlite3")), CodexConnections(), run_worker=False
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).resolve().parents[1] / "openapi.json"
    )
    args = parser.parse_args()
    expected = contract()
    if args.check:
        if args.output.read_text() != expected:
            parser.exit(1, "OpenAPI is stale; run python -m tools.export_openapi\n")
    else:
        args.output.write_text(expected)


if __name__ == "__main__":
    main()

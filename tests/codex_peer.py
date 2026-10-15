"""Read-only app-server peer used by the composed-service acceptance fixture."""

import json
import sys
from pathlib import Path


def serve(state_path: str) -> None:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        state = json.loads(Path(state_path).read_text())
        method = request["method"]
        if method == "initialize":
            result = {}
        elif method == "account/rateLimits/read":
            result = state["usage"]
        elif method == "thread/list":
            result = {"data": [state["summary"]], "nextCursor": None}
        elif method == "thread/read":
            result = {"thread": state["summary"]}
        else:
            raise AssertionError(f"The monitoring helper attempted execution: {method}")
        print(json.dumps({"id": request["id"], "result": result}), flush=True)

"""Read-only SDK peer used by the composed-service acceptance fixture."""

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
        summary = {
            "preview": "",
            "modelProvider": "openai",
            "createdAt": 1,
            "cliVersion": "0.162.1",
            "ephemeral": False,
            "sessionId": "session-a",
            "source": "appServer",
            "status": {"type": "notLoaded"},
            "turns": [],
            **state["summary"],
        }
        if method == "initialize":
            result = {"userAgent": "codex/0.162.1"}
        elif method == "account/rateLimits/read":
            result = state["usage"]
            buckets = result.get("rateLimitsByLimitId", {"codex": result.get("rateLimits")})
            for key, bucket in buckets.items():
                bucket["limitId"] = key
                for window in ("primary", "secondary"):
                    if bucket.get(window):
                        bucket[window].setdefault("windowDurationMins", 300)
            result.setdefault("rateLimits", buckets["codex"])
        elif method == "thread/list":
            result = {"data": [summary], "nextCursor": None}
        elif method == "thread/read":
            assert request["params"]["includeTurns"] is False
            result = {"thread": summary}
        elif method == "thread/turns/list":
            assert request["params"]["itemsView"] == "notLoaded"
            result = {
                "data": [
                    {
                        "id": "accepted-turn" if state.get("accepted_id") else "original-turn",
                        "status": "failed",
                        "completedAt": 100,
                        "items": [],
                        "itemsView": "notLoaded",
                        "error": {"message": "Quota", "codexErrorInfo": "usageLimitExceeded"},
                    }
                ]
            }
        elif method == "thread/items/list":
            assert request["params"]["limit"] == 1
            result = {
                "data": [
                    {
                        "turnId": request["params"]["turnId"],
                        "item": {
                            "type": "userMessage",
                            "id": "item-a",
                            "clientId": state.get("accepted_id", "original"),
                            "content": [{"type": "text", "text": "Task", "text_elements": []}],
                        },
                    }
                ]
            }
        elif method == "thread/goal/get":
            result = {"goal": None}
        else:
            raise AssertionError(f"Unexpected monitoring request: {method}")
        reply = json.dumps({"id": request["id"], "result": result})
        with Path(state_path).with_suffix(".requests.jsonl").open("a") as log:
            log.write(
                json.dumps(
                    {
                        "method": method,
                        "params": request.get("params"),
                        "reply_bytes": len(reply.encode()),
                    }
                )
                + "\n"
            )
        print(reply, flush=True)

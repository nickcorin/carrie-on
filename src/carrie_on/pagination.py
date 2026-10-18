"""Opaque pagination tokens bound to an application's list query."""

import base64
import json
from typing import Any

from carrie_on.queue import QueueError


def encode_cursor(scope: list[Any], position: Any) -> str:
    payload = json.dumps([scope, position], separators=(",", ":"), allow_nan=False).encode()
    return base64.urlsafe_b64encode(payload).decode()


def decode_cursor(cursor: str, scope: list[Any]) -> Any:
    try:
        saved_scope, position = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if saved_scope != scope:
            raise ValueError("Query changed")
        return position
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise QueueError("invalid_cursor", "Cursor does not belong to this query") from error

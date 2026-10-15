"""HTTP validation, serialization, and resource contracts for the local service."""

import re
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Header, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from starlette.exceptions import HTTPException

from carrie_on.application import QueueService
from carrie_on.codex.models import CodexProtocolError, CodexUnavailable, UsageSnapshot
from carrie_on.dispatch import DispatchWorker
from carrie_on.ports import StorageUnavailable
from carrie_on.queue import (
    DEFAULT_INSTRUCTION,
    PENDING_STATES,
    EntryState,
    QueueEntry,
    QueueError,
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Instruction(Input):
    instruction: StrictStr = Field(min_length=1, max_length=32000)

    @field_validator("instruction")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Instruction cannot be blank")
        return value


class Enqueue(Instruction):
    id: UUID
    chat_id: UUID
    instruction: StrictStr = Field(default=DEFAULT_INSTRUCTION, min_length=1, max_length=32000)
    limit_id: StrictStr | None = Field(default=None, min_length=1, max_length=200)


class QueueEntryResponse(BaseModel):
    id: UUID
    chat_id: UUID
    title: str
    account_id: str
    limit_id: str
    anchor_message_id: str | None
    anchor_turn_id: str | None
    model: str
    instruction: str
    reset_windows: dict[str, int]
    enrolled_while_busy: bool
    state: EntryState
    error: str
    revision: int
    queued_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    dispatching: bool


class QueuePage(BaseModel):
    items: list[QueueEntryResponse]
    next_cursor: str | None


class ChatSummary(BaseModel):
    id: str
    title: str | None
    cwd: str | None
    last_activity: float | None


class ChatPage(BaseModel):
    items: list[ChatSummary]
    next_cursor: str | None


class HealthResponse(BaseModel):
    version: str
    build_id: str | None
    worker_healthy: bool
    worker_error: str | None


class ConnectionResponse(BaseModel):
    connected: bool


class ResolvedChat(BaseModel):
    id: UUID


class QuotaWindow(BaseModel):
    usedPercent: float
    resetsAt: int
    windowDurationMins: int | None


class QuotaBucket(BaseModel):
    primary: QuotaWindow | None = None
    secondary: QuotaWindow | None = None
    spendControlReached: bool
    rateLimitReachedType: str | None


class UsageResponse(BaseModel):
    accountId: str
    ordinaryUsageAllowed: bool
    rateLimitsByLimitId: dict[str, QuotaBucket]


class UsageWindow(BaseModel):
    bucket: str
    name: str
    used_percent: float
    resets_at: int
    window_minutes: int | None


class UsageSummary(BaseModel):
    windows: list[UsageWindow]


class QueueSummary(BaseModel):
    count: int
    needs_attention: int
    counts: dict[EntryState, int]
    dispatching: int


class StatusResponse(BaseModel):
    connected: bool
    usage: UsageSummary | None
    queue: QueueSummary | None
    usage_error: str | None
    queue_error: str | None
    worker_healthy: bool


class Readiness(BaseModel):
    id: UUID
    title: str
    outcome: Literal["ready", "sent", "stale", "failed"]
    reason: str


class ReadinessPage(BaseModel):
    items: list[Readiness]
    next_cursor: str | None


class ChatDiagnostic(BaseModel):
    title: str
    status: str
    model: str


class DiagnosticResponse(BaseModel):
    usage_read: Literal["ok"]
    desktop_ipc: Literal["ok"]
    model_turns_started: Literal[0]
    account_id: str | None
    chat: ChatDiagnostic | None = None


class PageQuery(Input):
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=16384)


class QueueQuery(PageQuery):
    state: Literal["open", "all", "queued", "sent", "stale", "cancelled", "failed"] = "open"
    chat_id: UUID | None = None


class ChatQuery(PageQuery):
    search: str = Field(default="", max_length=500)
    limit: int = Field(default=25, ge=1, le=100)


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str


def problem(code, detail, status):
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"urn:carrie-on:problem:{code}",
            "title": code.replace("_", " ").capitalize(),
            "status": status,
            "detail": detail,
            "code": code,
        },
    )


def queue_response(entry: QueueEntry) -> dict:
    """Publish entry state without exposing delivery ownership or creation identity."""
    fields = asdict(entry)
    fields["instruction"] = fields.pop("instructions")
    fields.pop("original_instructions")
    fields.pop("requested_limit_id")
    return fields


def usage_response(usage: UsageSnapshot) -> dict:
    return {
        "accountId": usage.account_id,
        "ordinaryUsageAllowed": usage.ordinary_usage_allowed,
        "rateLimitsByLimitId": {
            bucket.limit_id: {
                "spendControlReached": bucket.spend_control_reached,
                "rateLimitReachedType": bucket.reached_type,
                **{
                    window.name: {
                        "usedPercent": window.used,
                        "resetsAt": window.reset_at,
                        "windowDurationMins": window.duration_minutes,
                    }
                    for window in bucket.windows
                },
            }
            for bucket in usage.buckets
        },
    }


def local_client(
    x_carrie_on: Annotated[
        Literal["1"], Header(description="Required local-client marker; send 1.")
    ],
):
    """Declare the marker checked by middleware before request processing."""


ETAG_HEADER = {
    "ETag": {
        "description": "Strong item revision; reuse as If-Match when editing.",
        "schema": {"type": "string", "example": '"revision-1"'},
    }
}
CREATION_HEADERS = {
    **ETAG_HEADER,
    "Location": {
        "description": "Canonical item resource, also returned on replay.",
        "schema": {"type": "string"},
    },
}


def create_app(
    service: QueueService,
    *,
    port: int,
    version: str,
    build_id: str | None = None,
    worker: DispatchWorker | None = None,
    lifespan=None,
) -> FastAPI:
    """Adapt supplied application operations to HTTP; construction has no storage/process I/O.

    The composition root owns initialization and worker lifetime through lifespan.
    A missing worker means read/command service mode with dispatch disabled.
    """
    errors: dict[int | str, dict[str, Any]] = {
        status: {"content": {"application/problem+json": {"schema": Problem.model_json_schema()}}}
        for status in (403, 404, 409, 412, 413, 415, 422, 428, 503)
    }
    app = FastAPI(
        title="Carrie On",
        version=version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        responses=errors,
        dependencies=[Depends(local_client)],
        description="Local queue service. Send X-Carrie-On: 1 on every request. "
        "Create with a client UUID; replay the same original payload after an uncertain response. "
        "Read an item's ETag before editing with If-Match. See ARCHITECTURE.md for examples.",
    )
    app.state.worker = worker
    origin = f"http://127.0.0.1:{port}"

    @app.middleware("http")
    async def local_only(request, call_next):
        headers = request.headers
        if (
            headers.get("host") != origin.removeprefix("http://")
            or headers.get("origin") not in (None, origin)
            or headers.get("sec-fetch-site") == "cross-site"
            or headers.get("x-carrie-on") != "1"
        ):
            response = problem("local_only", "Request must come from the local app", 403)
        elif len(request.query_params) != len(request.query_params.multi_items()):
            response = problem("invalid_query", "Repeated query parameters are unsupported", 422)
        else:
            response = None
            if request.method in {"POST", "PATCH", "PUT"}:
                expected = (
                    "application/merge-patch+json"
                    if request.method == "PATCH"
                    else "application/json"
                )
                if headers.get("content-type", "").split(";", 1)[0] != expected:
                    response = problem("media_type", f"Expected {expected}", 415)
                else:
                    chunks, length = [], 0
                    async for chunk in request.stream():
                        length += len(chunk)
                        if length > 150000:
                            response = problem(
                                "body_too_large", "Request exceeds 150,000 bytes", 413
                            )
                            break
                        chunks.append(chunk)
                    if response is None:
                        request._body = b"".join(chunks)
            if response is None:
                response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            }
        )
        return response

    @app.exception_handler(QueueError)
    async def queue_error(_, error):
        status = {
            "not_found": 404,
            "invalid_cursor": 422,
            "stale_revision": 412,
            "precondition_required": 428,
            "invalid_chat": 422,
            "invalid_bucket": 422,
            "stale_usage": 422,
            "chat_absent": 422,
        }.get(error.code, 409)
        return problem(error.code, str(error), status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, error):
        if any(
            value["loc"] == ("header", "if-match") and value["type"] == "missing"
            for value in error.errors()
        ):
            return problem("precondition_required", "Include the item's ETag in If-Match", 428)
        details = "; ".join(
            f"{'.'.join(str(part) for part in value['loc'])}: {value['msg']}"
            for value in error.errors()
        )
        return problem("invalid_request", details, 422)

    @app.exception_handler(HTTPException)
    async def http_error(_, error):
        response = problem(
            "not_found" if error.status_code == 404 else "http_error",
            str(error.detail),
            error.status_code,
        )
        response.headers.update(error.headers or {})
        return response

    async def unavailable(_, error):
        return problem("unavailable", str(error) or type(error).__name__, 503)

    for error_type in (CodexUnavailable, CodexProtocolError, StorageUnavailable):
        app.add_exception_handler(error_type, unavailable)

    @app.get(
        "/api/v1/health",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse, **errors[503]}},
    )
    def health(response: Response):
        service.store.get_queue_summary()
        worker = app.state.worker
        healthy = worker is None or worker.healthy
        response.status_code = 200 if healthy else 503
        return {
            "version": version,
            "build_id": build_id,
            "worker_healthy": healthy,
            "worker_error": worker.error if worker else None,
        }

    @app.get("/api/v1/status", response_model=StatusResponse)
    def status():
        result = service.status()
        windows = (
            [
                {
                    "bucket": bucket.limit_id,
                    "name": window.name,
                    "used_percent": window.used,
                    "resets_at": window.reset_at,
                    "window_minutes": window.duration_minutes,
                }
                for bucket in result.usage.buckets
                for window in bucket.windows
            ]
            if result.usage
            else None
        )
        return {
            "connected": result.connected,
            "usage": {"windows": windows} if windows is not None else None,
            "queue": {
                **asdict(result.queue),
                "count": result.queue.count,
                "needs_attention": result.queue.needs_attention,
            }
            if result.queue
            else None,
            "usage_error": result.usage_error,
            "queue_error": result.queue_error,
            "worker_healthy": worker is None or worker.healthy,
        }

    @app.get("/api/v1/connection", response_model=ConnectionResponse)
    def connection():
        return {"connected": service.connected()}

    @app.get("/api/v1/usage", response_model=UsageResponse)
    def usage():
        return usage_response(service.read_usage())

    @app.get("/api/v1/chats/resolve", response_model=ResolvedChat)
    def resolve(query: Annotated[str, Query(min_length=1, max_length=500)]):
        return {"id": service.resolve_chat(query)}

    @app.get("/api/v1/chats", response_model=ChatPage)
    def chats(query: Annotated[ChatQuery, Query()]):
        return asdict(service.chats(**query.model_dump()))

    @app.get("/api/v1/queue/metadata", response_model=ChatPage)
    def metadata(query: Annotated[PageQuery, Query()]):
        return asdict(service.queued_chats(**query.model_dump()))

    @app.get("/api/v1/queue/check", response_model=ReadinessPage)
    def inspect(query: Annotated[PageQuery, Query()]):
        values, cursor = service.inspect(**query.model_dump())
        return {
            "items": [
                {"id": value.id, "title": value.title, **asdict(value.eligibility)}
                for value in values
            ],
            "next_cursor": cursor,
        }

    @app.get("/api/v1/diagnostics", response_model=DiagnosticResponse)
    def doctor(chat_id: UUID | None = None):
        report = service.doctor(str(chat_id) if chat_id else None)
        chat = report.chat
        return {
            "usage_read": "ok",
            "desktop_ipc": "ok",
            "model_turns_started": 0,
            "account_id": report.account_id,
            "chat": {"title": chat.title, "status": chat.status, "model": chat.model}
            if chat
            else None,
        }

    @app.get("/api/v1/queue", response_model=QueuePage)
    def queue(query: Annotated[QueueQuery, Query()]):
        states = (
            None
            if query.state == "all"
            else (PENDING_STATES + ("stale",) if query.state == "open" else (query.state,))
        )
        items, cursor = service.store.list_entries(
            states=states, chat_id=query.chat_id, limit=query.limit, cursor=query.cursor
        )
        return {"items": [queue_response(item) for item in items], "next_cursor": cursor}

    @app.post(
        "/api/v1/queue",
        response_model=QueueEntryResponse,
        status_code=201,
        responses={
            200: {"model": QueueEntryResponse, "headers": CREATION_HEADERS},
            201: {"headers": CREATION_HEADERS},
        },
    )
    def enqueue(body: Enqueue, response: Response):
        entry, created = service.enqueue(
            body.id, body.chat_id, body.instruction, limit_id=body.limit_id
        )
        response.status_code = 201 if created else 200
        response.headers["Location"] = f"/api/v1/queue/{entry.id}"
        response.headers["ETag"] = f'"revision-{entry.revision}"'
        if created and app.state.worker:
            app.state.worker.wakeup.set()
        return queue_response(entry)

    @app.get(
        "/api/v1/queue/{item_id}",
        response_model=QueueEntryResponse,
        responses={200: {"headers": ETAG_HEADER}},
    )
    def item(item_id: UUID, response: Response):
        value = service.store.get_entry(item_id)
        if value is None:
            raise QueueError("not_found", "Queue entry not found")
        response.headers["ETag"] = f'"revision-{value.revision}"'
        return queue_response(value)

    @app.patch(
        "/api/v1/queue/{item_id}",
        response_model=QueueEntryResponse,
        responses={200: {"headers": ETAG_HEADER}},
    )
    def edit(
        item_id: UUID,
        body: Annotated[Instruction, Body(media_type="application/merge-patch+json")],
        response: Response,
        if_match: Annotated[
            str,
            Header(description="Current strong ETag from GET; required to prevent lost updates."),
        ],
    ):
        matched = re.fullmatch(r'"revision-([1-9][0-9]{0,18})"', if_match)
        if not matched:
            raise QueueError("stale_revision", "Read the item and use its current ETag")
        value = service.store.update_instructions(
            item_id, body.instruction, revision=int(matched[1])
        )
        response.headers["ETag"] = f'"revision-{value.revision}"'
        return queue_response(value)

    @app.post("/api/v1/queue/{item_id}/cancel", response_model=QueueEntryResponse)
    def cancel(item_id: UUID, body: Input):
        return queue_response(service.store.update_entry_state(item_id, "cancelled"))

    return app

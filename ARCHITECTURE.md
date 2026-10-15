# Application architecture

Carrie On is a local client/server application. The native Swift app and Typer CLI use the same FastAPI service. The service owns SQLite and runs a supervised dispatch worker. Clients never read the database or launch a competing worker.

## Project structure

| Location | Responsibility |
| --- | --- |
| `src/carrie_on/queue.py` | Queue items, deliveries, states, and reset eligibility |
| `src/carrie_on/storage.py` | SQLite schema, explicit row mappings, transactions, queries, leases, and atomic import |
| `src/carrie_on/legacy.py` | Retired JSON decoding, merge precedence, and normalized import records |
| `src/carrie_on/application.py` | Enrollment and read operations using Codex observations |
| `src/carrie_on/dispatch.py` | Delivery orchestration and supervised worker lifetime |
| `src/carrie_on/api.py` | HTTP contract, input validation, response serialization, and error mapping |
| `src/carrie_on/server.py` | Configuration, collaborator construction, persistence startup, and process entrypoint |
| `src/carrie_on/cli.py` | Typer command tree and HTTP client |
| `src/carrie_on/codex/` | Validated observations and private desktop/read-only app-server adapters |
| `src/carrie_on/macos/` | Installation, lifecycle, update, compatibility, and older-installation migration |
| `menubar/` | Native presentation, HTTP client, and macOS app lifecycle |
| `tools/` | Build, icon, release, OpenAPI, and package-verification tooling |
| `tests/` | Domain, storage, HTTP, CLI, protocol, packaging, and native behavior checks |

The domain and application layers do not depend on FastAPI or Typer. The service owns decisions; adapters translate inputs and results. Network operations occur outside database transactions. Every SQLite operation uses its own connection, so a slow Codex call does not prevent another client from reading or changing unrelated work. `pyproject.toml` installs the `src` package and exposes `carrie-on` and `carrie-on-server`.

`EnqueueCommand` retains the original creation identity. `QueueItem` represents durable intent; `QueueEntry` groups it with an optional `ActiveDelivery` for reads. The HTTP serializer flattens that relationship for clients. Eligibility is a pure function of typed observations. Codex adapters normalize protocol variants into complete quota, catalog, and chat records, and return an accepted turn ID after sending. Expected connection failures, malformed upstream data, and domain rejections have distinct exception types; programming and persistence failures reach worker supervision with their tracebacks.

The server constructs a shared connection configuration, while every operation owns separate context-managed connections. Platform entrypoints resolve `Runtime` and validated `Settings` explicitly; importing a component does not select a profile or inspect an installed app. Both installation paths load settings under the same installation lock and publish the same launchd configuration. Module dependency tests and `just typecheck` guard these contracts. `just reference` generates Python API pages and native symbol graphs under `build/reference/`.

## Durable state

`queue_items` records intent, exact instructions, chat/task/model/account anchors, reset windows, state, revision, and `deferrals`. `deliveries` records each attempt, its instruction snapshot, UUID, lease expiry, send-start time, receipt, and outcome. `metadata` records schema-import markers and last-check time. IDs for new items and deliveries are UUIDs. A partial unique index permits only a single queued item for a chat; another permits only a single unfinished delivery per item.

SQLite runs in WAL mode with foreign keys, full synchronization, a five-second busy timeout, and short `BEGIN IMMEDIATE` writes. Claiming selects a queued item without an unexpired delivery, ordered by `deferrals`, enqueue timestamp, and UUID. The claim closes any expired attempt and creates a delivery atomically. Deferring increments that item's `deferrals`; the counter is neither globally unique nor a sequence. New items start at the current minimum deferral count. There is no per-item next-check timestamp.

The worker processes an item at a time. A delivery lease lasts 120 seconds, with a 100-second external-operation budget. Sending and completion require the same unexpired delivery UUID. A stale worker cannot finish another worker's attempt. The worker waits between selections, targeting the configured interval for a round through the queue, with a minimum one-second wait. Fatal worker failure makes health fail and requests process exit; launchd restarts the service. Shutdown is bounded, and abandoned leases recover on a later loop.

The queue item UUID is also the outgoing client message ID. Every attempt observes fresh Codex state. An already-observed message is recorded as sent; a changed task or model is recorded as changed. After an uncertain reply or a process crash, the next attempt checks again before sending. SQLite cannot make a transaction atomic with Codex, so this is recoverable delivery with duplicate suppression, not an exactly-once guarantee.

## HTTP contract

The service binds to `127.0.0.1`. Requests require the exact configured Host and `X-Carrie-On: 1`; foreign origins and cross-site requests are rejected. Responses disable caching. JSON writes have a bounded body size and reject unknown fields. Errors use `application/problem+json` with `type`, `title`, `status`, `detail`, and stable `code` fields. The generated [OpenAPI contract](openapi.json) describes request and response schemas.

| Method and path | Behavior |
| --- | --- |
| `GET /api/v1/health` | Database schema and worker health; no Codex calls |
| `GET /api/v1/status` | Compact connection, usage, and queue summary for the menu |
| `GET /api/v1/connection` | Bounded desktop handshake |
| `GET /api/v1/usage` | Account and quota observations |
| `GET /api/v1/chats` | Searchable, paginated local catalog |
| `GET /api/v1/chats/resolve?query=...` | Resolve UUID, deep link, or exact unique title |
| `GET /api/v1/queue` | Paginated visible items; filter by state/chat or request all history |
| `POST /api/v1/queue` | Create using a caller-provided UUID, chat ID, instruction, and optional quota bucket |
| `GET /api/v1/queue/{id}` | Read an item and its ETag |
| `PATCH /api/v1/queue/{id}` | Edit the instruction with merge-patch JSON and `If-Match` |
| `POST /api/v1/queue/{id}/cancel` | Cancel/dismiss with an empty JSON object, preserving history |
| `GET /api/v1/queue/{id}/deliveries` | Paginated attempt history |
| `GET /api/v1/queue/metadata` | Independent queued-chat metadata, with offline fallback |
| `GET /api/v1/queue/check` | Paginated read-only readiness inspection |
| `GET /api/v1/diagnostics` | Read-only connectivity/usage checks, optionally for a chat |

Creation returns 201 and Location, or 200 when the same UUID and original input are replayed. Reusing a UUID with different input returns 409. Replay remains valid after edits and terminal outcomes. Clients retain the UUID after an unknown result. Active deliveries block edits and cancellation with 409. Missing `If-Match` returns 428; stale revisions return 412. A sent instruction cannot be cancelled. Repeated cancellation is safe.

For example, read the item before editing and copy its exact ETag into the update:

```http
GET /api/v1/queue/8edbfa77-c329-48dd-ad72-121987f7f940 HTTP/1.1
Host: 127.0.0.1:8766
X-Carrie-On: 1
```

A response with `ETag: "revision-7"` permits this conditional update:

```http
PATCH /api/v1/queue/8edbfa77-c329-48dd-ad72-121987f7f940 HTTP/1.1
Host: 127.0.0.1:8766
X-Carrie-On: 1
Content-Type: application/merge-patch+json
If-Match: "revision-7"

{"instruction":"Finish the remaining tests."}
```

After a 412 response, read the item again before deciding whether to apply the edit. After an uncertain creation, get the original UUID or repeat the exact original POST payload. A failed read does not establish that creation failed. Health returns a health representation with 503 when its worker fails; database unavailability returns a 503 Problem. Both representations are declared in OpenAPI.

Lists use opaque query-scoped cursors and bounded page sizes. Cursors are not snapshots: concurrent changes can move items, so clients deduplicate UUIDs. The default queue view includes queued items and the latest changed/failed item for each chat; `state=all` retains every outcome. Re-enrolling a chat removes its earlier warning from the default view without deleting history.

## Installation and migration

The backend and menu have separate launchd registrations. The backend replaces both the old scheduled worker and HTTP process; setup retires the old writer before importing its queue. Release and Dev use independent databases, settings, services, logs, and ports.

On first start, the database imports the adjacent `resume-queue.json` transactionally and leaves it untouched for recovery. UUIDs, anchors, instructions, and complete history survive. Old in-flight attempts become expired deliveries; held entries become changed items. Partial historical records are retained verbatim in database metadata because they lack dispatch anchors. Pause settings are ignored. A durable import marker prevents replay on restart. Invalid active data aborts import and rolls back rather than starting with an empty queue.

Older installation migration retains its recovery journal and immutable source snapshots. Portable conversion produces normalized import records directly; the platform coordinator does not stage an operational JSON queue. Import into an existing database preserves current SQLite work, keeps conflicting older queued work as cancelled history, and never reactivates an existing UUID. Schema version 2 upgrades the prototype SQLite schema atomically while preserving queue and delivery records. Downgrades do not export new SQLite state to the old JSON file; back up Application Support before changing data formats. See [source installation modes](INSTALL.md) for direct and registered service data locations.

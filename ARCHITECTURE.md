# Application architecture

Carrie On is a local client/server application. The native Swift app and Typer CLI use the same FastAPI service. The service owns SQLite and runs a supervised dispatch worker. Clients never read the database or launch a competing worker.

## Project structure

| Location | Responsibility |
| --- | --- |
| `src/carrie_on/queue.py` | QueueEntry, DeliveryAttempt, lifecycle transitions, and reset eligibility |
| `src/carrie_on/ports.py` | Application-owned storage and maintenance protocols |
| `src/carrie_on/storage/` | Declarative SQL migrations, private connections, row mappings, and atomic queries |
| `src/carrie_on/legacy.py` | Retired JSON decoding, merge precedence, and import orchestration through the storage protocol |
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

`QueueEntry` represents captured intent and lifecycle state. It owns enrollment, immutable instruction/state changes, and eligibility decisions over typed observations. UUIDs and aware UTC datetimes remain typed until an adapter serializes them. Instructions have a single current value. Request replay belongs to the HTTP boundary. `DeliveryAttempt` is an immutable lease containing an entry snapshot and expiry. Clients receive a derived `dispatching` flag; attempts are private to the worker that creates them.

Application code depends on `QueueStorage`, not its SQLite implementation. `SqliteQueueStorage` owns both entries and attempts because acquiring work requires an atomic query across both tables. Domain rules live on `QueueEntry`; storage enforces uniqueness, revisions, and exclusive delivery acquisition. Connections, transactions, SQL, and SQLite errors never cross `storage/`. `StorageUnavailable` represents persistence failure; `QueueError` identifies an expected rejection. Codex connection failures and malformed upstream data remain distinct from programming errors.

The read adapter uses the official Codex Python SDK. It reads compact thread metadata, a latest-turn header, and a bounded opening-message page. Missing completion evidence means the chat is busy. It pages older headers only when reconciling a changed conversation against an uncertain send. Desktop IPC reads settings and sends through the owning app; it never subscribes to full conversation history.
The server constructs a shared connection configuration, while every operation owns separate context-managed connections. Platform entrypoints resolve `Runtime` and validated `Settings` explicitly; importing a component does not select a profile or inspect an installed app. Both installation paths load settings under the same installation lock and publish the same launchd configuration. Module dependency tests and `just typecheck` guard these contracts. `just reference` generates Python API pages and native symbol graphs under `build/reference/`.

## Storage services

The caller-facing protocol is in `ports.py`; `storage/queue.py` implements it with parameterized queries and explicit row mappings:

```python
get_entry(entry_id: UUID) -> QueueEntry | None
list_entries(*, states=("queued", "failed"), chat_id=None, limit=50, cursor=None)
    -> tuple[list[QueueEntry], str | None]
get_queue_summary() -> QueueSummary
create_entry(entry: QueueEntry) -> QueueEntry
update_instructions(entry_id: UUID, instructions: str, *, revision: int) -> QueueEntry
update_entry_state(entry_id: UUID, state: EntryState, *, error: str = "") -> QueueEntry
create_delivery_attempt() -> DeliveryAttempt | None
```

`get_entry` returns `None` for an unknown UUID; mutations reject missing entries. Creation inserts a record and returns its persisted state. Duplicate UUIDs raise `entry_exists`; conflicting pending chats raise `chat_already_queued`. Storage does not interpret request replays or import batches. Edits compare the supplied current revision atomically. A repeated identical state update is a no-op.

`QueueSummary` contains counts for each state and the number currently dispatching, with pending-count and attention-count properties. `list_entries` supports 1–100 records per page, query-bound opaque cursors, and `states=None` for history. Results are live observations rather than a transaction spanning multiple calls.

`SqliteStorageMaintenance` implements `initialize()` and `create_backup(destination)`. `storage/database.py` owns connections and transaction lifetimes. `storage/schema.py` runs Yoyo against the declarative files in `storage/migrations/`. Startup applies pending schema migrations under the migration lock, then requires the current schema. Query code has no paths for older schemas. Unknown/newer migrations fail startup; JSON conversion lives outside this package.

## Durable state and dispatch

`queue_entries` stores instructions, conversation/model/account anchors, quota-window reset timestamps, state, last error, revision, and timestamps. `delivery_attempts` stores immutable UUIDs, entry references, creation times, and expiries. A partial unique index allows a pending entry per chat; a constraint trigger rejects overlapping attempts for the same entry.

Enrollment reads fresh usage and captures each named window's `resetsAt` timestamp in `reset_windows`. Within the captured account and quota bucket, a different timestamp identifies a different quota window. Sending requires a changed timestamp in any captured window, current usage permission, and headroom in every reported window. The new timestamp need not be later than the saved timestamp, and the saved deadline need not have elapsed. Percentage decreases and exhaustion observations do not establish window identity. Existing saved reset timestamps retain the same representation through schema upgrades and external imports.

SQLite uses WAL, foreign keys, full synchronization, a five-second busy timeout, and short write transactions. Acquisition selects the next `queued` or `failed` entry without a live attempt, ordered by `(queued_at, id)`, and inserts its attempt in the same transaction. There are no deferrals or per-entry check timestamps. Other entries remain available while an attempt is active.

The worker's `send()` owns observation and delivery. An attempt lasts 120 seconds; external operations have a budget of at most 100 seconds, bounded by remaining ownership. The worker reads fresh quota and conversation state before sending. Busy chats, unavailable quota, model changes, and transport failures become `failed` with an error and may retry after expiry. A newer conversation anchor becomes terminal `stale`. Recorded acceptance becomes `sent`. `cancelled` never retries; its optional error distinguishes a terminal failure from user removal. State changes do not mutate attempts or release ownership early.

The entry UUID is the outgoing client message ID. Accepted-message reconciliation takes precedence over stale detection. After an uncertain reply or process crash, another attempt checks Codex again before sending. A database transaction cannot be atomic with the desktop send, so this is recoverable delivery with duplicate suppression. Entry updates do not carry an attempt token; they do not fence a worker that has outlived its lease.

The worker waits between selections, targeting the configured interval for a pass through pending work with a minimum one-second wait. Fatal worker failure makes health fail and requests process exit; launchd restarts the service. Shutdown is bounded, and abandoned attempts expire naturally.

## HTTP contract

The service binds to `127.0.0.1`. Requests require the exact configured Host and `X-Carrie-On: 1`; foreign origins and cross-site requests are rejected. Responses disable caching. JSON writes have a bounded body size and reject unknown fields. Errors use `application/problem+json` with `type`, `title`, `status`, `detail`, and stable `code` fields. The generated [OpenAPI contract](openapi.json) describes request and response schemas.

| Method and path | Behavior |
| --- | --- |
| `GET /api/v1/health` | Storage availability and worker health; no Codex calls |
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
| `GET /api/v1/queue/metadata` | Independent queued-chat metadata, with offline fallback |
| `GET /api/v1/queue/check` | Paginated read-only readiness inspection |
| `GET /api/v1/diagnostics` | Read-only connectivity/usage checks, optionally for a chat |

Creation returns 201 and Location, or 200 with the current entry when its UUID is replayed. The HTTP boundary resolves concurrent creation conflicts by reading that UUID. Replays never change instructions, quota selection, or state; the first successful creation wins. Using an existing UUID for another chat returns 409. Replay remains valid after edits, terminal outcomes, and server restarts. Clients retain the UUID after an unknown result and use PATCH for instruction edits. Active deliveries block edits and cancellation with 409. Missing `If-Match` returns 428; stale revisions return 412. A sent instruction cannot be cancelled. Repeated cancellation is safe.

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

Lists use opaque query-scoped cursors and bounded page sizes. Cursors are not snapshots: concurrent changes can move items, so clients deduplicate UUIDs. The default queue view includes pending and stale entries; `state=all` retains every outcome. Stale warnings can be dismissed without deleting history. Attempts have no public read or mutation endpoints. Clients store their own refresh/check time.

## Installation and migration

The backend and menu have separate launchd registrations. The backend replaces both the old scheduled worker and HTTP process; setup retires the old writer before importing its queue. Release and Dev use independent databases, settings, services, logs, and ports.

Before serving requests, the composition root initializes the schema and invokes `LegacyImporter` for an adjacent `resume-queue.json`. The importer decodes JSON into current `QueueEntry` values, reconciles them against current work, and inserts entries individually. It preserves UUIDs, exact instructions, both supported conversation identities, and complete entry history. Recorded acceptance imports as sent; uncertain work is checked again before sending. Pause settings are ignored.

The original JSON remains unchanged and retains partial history and historical attempt details that are no longer part of current storage. Immutable installation snapshots and the recovery journal protect retries. Existing UUIDs are skipped, preserving edits and terminal outcomes across interrupted imports. A failure may leave some entries imported; retry continues with the missing entries. Malformed active records are rejected during decoding, before insertion.

Older-installation migration uses the same importer and storage backup service. Current work wins over conflicting older enrollment; older records remain cancelled history. Schema migrations only transform database representation. They contain no Codex calls, JSON-file import, installation decisions, or runtime compatibility branches. Downgrades do not export SQLite state back to JSON. See [installation modes](INSTALL.md) for data locations.

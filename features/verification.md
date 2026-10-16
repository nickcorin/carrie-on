# Feature verification

The checks below cover product behavior through the current storage protocols and clients. Queue pause, public attempt history, and deferral counters are absent. Automated tests use temporary homes/databases, synthetic Codex adapters, local HTTP/Unix sockets, and native test executables. They never send a real instruction or change an installed app. Manual acceptance steps live in the relevant feature maps.

| Feature | Verification |
| --- | --- |
| Exact title, UUID, and deep-link enrollment; ambiguous title rejection | Application and CLI tests |
| Default/custom instruction; file/stdin input; Unicode and multiline text | CLI HTTP integration and native self-tests |
| Duplicate chat rejection and incomplete-anchor rejection | API concurrent enrollment and domain validation |
| Model, task, account, bucket, and reset anchors | Dispatch/domain tests |
| Changed reset timestamp plus immediately usable quota; unchanged timestamps despite percentage drops or elapsed deadlines; earlier/later timestamps, multiple windows, new window names, changed account, restart without observed exhaustion | Dispatch tests and HTTP enrollment checks |
| Busy work, approvals/input, blocked goals, normal completion, resuming failed/interrupted work without a quota error | Dispatch tests |
| Dormant chat validation before opening; archived chats refused | Unix-socket desktop protocol tests and dispatch tests |
| Desktop settings and permissions retained; helper cannot start turns | Protocol and adapter tests |
| Fresh enrollment usage, resets before enrollment, failed reads, out-of-order observations | API tests |
| Slow enrollment does not block unrelated queue actions | Concurrent API test with held Codex reads |
| Queue read, instruction edit, cancellation, history, and repeat cancellation | API/CLI/storage tests; native URLSession and CLI against the same HTTP service and SQLite database |
| Conflicting edits and stale item identities | Concurrent HTTP edits with the same body revision permit only one write; API and native checks verify revision conflicts and active delivery protection; cancel/re-enroll between actual HTTP pages retains distinct queue identities |
| Read-only diagnostics and dry-run readiness | API/CLI tests assert no deliveries or sends |
| Delivery failures remain visible and temporary failures retry | Dispatch and native state mapping |
| Lost send reply, already-observed message, and changed conversation | Dispatch tests plus real HTTP, lifespan worker, app-server subprocess, desktop socket, and persisted entry/restart acceptance |
| Process death, lease expiry, competing attempt acquisition | SQLite tests, including a child process exiting after claim |
| Queue ordering, equal timestamps, unrelated work during active ownership | SQLite ordering tests |
| Idempotent creation after lost HTTP reply, edits, cancellation, and restart | Actual CLI-to-HTTP timeout and SIGINT tests; native persistence through a 404 lookup, rejected 422 replay, and restart; native recovery of a committed creation after HTTP 503; HTTP replay and storage uniqueness tests |
| SQLite durability, private files, reader concurrency, transaction rollback | Storage tests |
| v0.5.7 JSON import, history, damaged records, retry, no resurrection | Raw v0.5.7 fixture through import and composed dispatch; turn-only anchors, changed turns with unchanged message IDs, already accepted messages, malformed history timestamps and IDs, NUL text, and interrupted per-entry import |
| Compact tray count/reset/usage, missing/invalid data, partial failures | Native self-tests and API summary tests |
| Usage selection, multiple buckets, countdown labels, activity times | Native self-tests |
| Offline/missing/closed/unresponsive Codex; reconnect and draft retention | Native connection checks and HTTP tests |
| Search, 25-item pages, more than 100 chats, late responses, overlapping pages, retry | Native pagination tests and API paging tests |
| Queued metadata independent of search; unavailable metadata fallback | API and native tests |
| Drafts, per-card action isolation, optimistic-state reconciliation | Native interaction tests |
| Separate sections, search/count/order, disclosure state, keyboard editing | Native model/self-tests; packaged Dev interaction verifies section disclosure, search, card flips, editor focus, Shift-Enter, Escape, and draft retention across search |
| Neutral/amber/blue/red cards, title/instruction/project/activity, spinner and errors | Native state tests, actual API dispatching/error decoding, and AppKit card rendering in light/dark fixtures |
| Hover controls, flips, scrollbars, animations, accessibility preferences | Light/dark card and full-picker renders; packaged Dev card/editor interaction and native accessibility page-up/page-down actions; broader assistive-technology and preference checks remain release acceptance |
| First launch, valid install locations, settings, CLI discovery, interrupted setup | Distribution tests and manual [package acceptance](distribution.md#package-acceptance) |
| Launch-at-login persistence, manual launch, quit, shutdown wait, reopen | Python registration tests and native lifecycle checks with a simulated launchd runner |
| Stable reopening, app replacement, port/socket/Codex-home preservation | Distribution tests |
| GitHub release selection, download integrity, offline failure, rollback, retry | Python/native updater tests with temporary apps |
| Source rebuild output, failure isolation, successful replacement, local commit discovery | Native/Python source-build tests |
| Release/Dev independent settings, state, ports, preferences, service targets | Build-profile and CLI service tests |
| App identity, release metadata, signed app, ZIP/DMG, relocation, no system Python | Build/release tests, packaging integrity checks, and manual [package acceptance](distribution.md#package-acceptance) |
| Loopback/Host/Origin/header restrictions, body bounds, validation, error schema | HTTP contract tests including required OpenAPI headers, health failure media, bounded cursors, positive integer revisions, and 405 Allow preservation |
| CLI help/version, structured stdout, stderr/errors, exit codes, explicit server | CLI subprocess tests, long Unicode add/edit round trips, HTTP 413 input rejection, and manual packaged CLI checks |
| Python module ownership and callable contracts | Dependency-boundary tests, Ruff formatting/import checks, and mypy on shared contracts |

## Acceptance boundaries

`tests/test_composed_service.py` exercises fresh enrollment and interrupted v0.5.7 JSON import with a desktop peer that would send a 33 MiB history if requested. It checks that dispatch reads compact SDK responses, never subscribes to desktop history, sends the exact instruction once, and preserves the sent entry across server restart. These checks run in the normal test suite.

Use the actual menu or CLI for [live dispatch acceptance](queue.md#live-dispatch-acceptance). The procedure requires an authorized disposable chat and a changed quota window with available usage.

`tests/test_architecture.py` rejects database libraries outside `storage/` and concrete storage imports from the application/domain. `tests/test_storage.py` verifies concurrent acquisition, process death, expiry, revisions, pagination, private backups, repeated schema initialization with existing work, interrupted per-entry import, and terminal states.

`just test` runs the Python and native automated tests. `just test-minimum` repeats them on Python 3.10. `just dev` additionally builds the complete Dev bundle, audits native dependencies, signs it, and checks the signature and mounted DMG. HTTP tests check FastAPI's live OpenAPI schema. Installing and exercising the packaged app follows [package acceptance](distribution.md#package-acceptance). `just lint` checks formatting, imports, workflow syntax, release metadata, and shared Python types.

`python tests/render_queue_cards.py OUTPUT.png [--dark]` renders real AppKit/SwiftUI cards with synthetic data. `.venv/bin/python tests/build_chat_scroll_preview.py /tmp/carrie-scroll --verify --snapshots /tmp/carrie-scroll/renders` runs the complete picker with 150 synthetic chats and 75 queued items. Repeated runs verify viewport pagination, search reset, beginning/end navigation, and independent overflowing lists; light/dark renders verify card spacing, alignment, and section heights. This fixture starts no services and rejects queue mutations.

For packaged Dev acceptance, use the app installed from the newly built DMG, independently of the release installation. Check the live connection and usage display, chat search, pagination past 50 chats, section disclosure, native accessibility page-up/page-down actions, card flipping without row selection, editor focus, multiline input, Escape, and draft retention across search. Clear search and draft text afterward. Confirm worker health, connection, and queue state through the installed CLI. This pass requires no enrollment or dispatch.

Automated lifecycle fixtures do not establish actual logout/login behavior. Synthetic Codex tests do not establish a future quota reset or a real send against the currently installed private protocol. Before release, complete the broader native gesture/accessibility preference checks, an authorized disposable-chat dispatch, actual login startup, Gatekeeper approval, and tests on another Mac including macOS 13. See [release acceptance](../INSTALL.md#release-verification). These are explicit manual gates, not claims made by the unit suite.

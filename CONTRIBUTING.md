# Contributing

Use an Apple Silicon Mac with Apple's command-line tools, just 1.58 or later, uv, Node.js, and Go. The backend uses FastAPI, SQLite, Typer, and the official Codex SDK. Runtime and build dependencies are pinned separately in `requirements.txt` and `requirements-build.txt`. See [build setup](RELEASING.md#toolchain).

```sh
git clone https://github.com/nickcorin/carrie-on.git
cd carrie-on
just setup
just ci
```

`just ci` runs all release checks locally, including tests on the minimum Python version and a signed, verified app build. Pushes and pull requests do not run GitHub Actions. Start the optional hosted workflow manually if needed. Use `just lint` or `just test` to check changes during development, and `just --list` to discover other commands.

Tests use temporary queues, fake Codex responses, loopback HTTP, and Unix sockets. They do not enroll or dispatch real chats. Allow local sockets in a sandbox. Native pagination tests compile a temporary Swift executable. Package verification uses an isolated home.

## Component map

| Component | Responsibility |
| --- | --- |
| `src/carrie_on/queue.py`, `ports.py` | Domain behavior and application-owned storage protocols |
| `src/carrie_on/storage/` | Declarative migrations, private connections, and atomic entry/attempt queries |
| `src/carrie_on/application.py`, `dispatch.py` | Enrollment, observations, readiness, and dispatch |
| `src/carrie_on/codex/` | Read-only app-server calls, validated snapshots, and desktop-owned dispatch |
| `src/carrie_on/cli.py` | Typer HTTP client and explicit service commands |
| `src/carrie_on/api.py`, `server.py` | HTTP contract and separate executable composition |
| `src/carrie_on/legacy.py` | Retired queue decoding, merge policy, and normalized imports |
| `menubar/` | Native AppKit menu, chat picker, setup, and lifecycle |
| `src/carrie_on/macos/` | Platform installation, migration, lifecycle, updates, and compatibility |
| `tools/` | Build, release, package acceptance, artwork, and OpenAPI export |
| `tests/` | Python and Swift behavior tests plus isolated visual fixtures |

Keep all database concepts inside `storage/`; application consumers depend only on `ports.py`. `SqliteQueueStorage` owns both entry and attempt tables and encapsulates their atomic queries. Domain rules belong on `QueueEntry`; external calls occur outside storage operations. Every send requires a freshly created, unexpired attempt and fresh Codex checks. Preserve original creation input for idempotent retries; edits require current revisions. Clients use HTTP exclusively. Add ordered SQL files under `storage/migrations/` for schema changes; never edit a migration after release. Yoyo applies pending files at startup. Runtime queries target only the current schema. JSON imports and installation reconciliation belong outside storage. Read the [architecture](ARCHITECTURE.md) and [feature contracts](features/index.md) before changing these boundaries.

`just setup` installs the `src` package in editable mode. Run tools as modules, such as `python -m tools.build_bundle`, and tests with `python -m unittest discover -v`. Regenerate `openapi.json` with `python -m tools.export_openapi` after contract changes; `just api-check` detects drift. The [verification matrix](features/verification.md) links retained behavior to its automated and manual checks.

`just lint-python` checks Ruff formatting and imports as well as correctness rules. `just typecheck` checks the shared Python contracts with mypy. Dependency tests prevent inner components from importing frameworks or platform configuration. `just reference` generates the Python interface reference at `build/reference/python/index.html` and native symbol graphs under `build/reference/native/`; inspect these after changing callable contracts.

## Changes

Use focused branches and Conventional Commits, and sign commits. Add behavior tests for changed contracts and update the relevant guide in the same change. For interactive testing, run `just dev` and install **Carrie On Dev.app** alongside the release app. Dev has separate data and services. Source installations share service labels with the release app. See [local development app instructions](RELEASING.md#local-development-app).

Open a pull request against `master` with the problem, resulting behavior, and test results. Run `just ci` locally before pushing and include the result in the pull request. No hosted check runs automatically. The release builder rejects incompatible Python runtimes through its Mach-O checks.

Never commit queue files, logs, settings, credentials, local verification records, or build environments. Use synthetic chat data in fixtures and screenshots. Bug reports should describe a minimal reproduction without private chat contents.

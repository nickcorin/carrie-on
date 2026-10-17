# Application architecture

The native menu and CLI use a local HTTP service. The service manages the queue, stores it in SQLite, and sends instructions through the Codex desktop app.

## Project structure

| Location | Purpose |
| --- | --- |
| `src/carrie_on/queue.py` | Queue entries and domain rules |
| `src/carrie_on/ports.py` | Storage protocols used by application code |
| `src/carrie_on/application.py` | Queue operations and Codex observations |
| `src/carrie_on/dispatch.py` | Instruction delivery and worker lifetime |
| `src/carrie_on/storage/` | SQL migrations, connections, transactions, and queries |
| `src/carrie_on/api.py` | HTTP routes and request validation |
| `src/carrie_on/server.py` | Configuration, dependency construction, and startup |
| `src/carrie_on/cli.py` | Terminal commands and HTTP client |
| `src/carrie_on/codex/` | Codex SDK and desktop protocol adapters |
| `src/carrie_on/legacy.py` | Import of the previous queue format |
| `src/carrie_on/macos/` | Installation, services, and app lifecycle |
| `menubar/` | Native Mac interface |
| `tools/` | Build and release commands |
| `tests/` | Automated checks and synthetic visual fixtures |

## Boundaries

Domain rules belong in `queue.py`. Application code uses the protocols in `ports.py` and does not import storage implementations. Database concepts stay inside `storage/`. Codex calls happen outside database transactions.

The API validates requests and translates application results into HTTP responses. Queue operations in the menu and CLI use that API. The server constructs the application's dependencies. Platform installation and service management stay separate from queue operations.

The [feature maps](features/index.md) define product behavior and verification.

## HTTP contract

FastAPI serves the OpenAPI schema at `/openapi.json`. With the release service running on its default port, read it with:

```sh
curl -H 'X-Carrie-On: 1' http://127.0.0.1:8766/openapi.json
```

Use port 8767 for Dev, or the configured port for a custom installation. The service accepts loopback requests and requires the `X-Carrie-On: 1` header. Routes are defined in `src/carrie_on/api.py`.

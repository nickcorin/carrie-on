# Contributing

Use an Apple Silicon Mac with Apple's command-line tools, just 1.58+, uv, Node.js, and Go. The application uses Python's standard library; build dependencies and lint tools are pinned. See [Releasing](RELEASING.md#toolchain) for setup.

```sh
git clone https://github.com/nickcorin/carrie-on.git
cd carrie-on
just setup
just ci
```

`just ci` runs all release gates locally, including minimum-Python tests and a signed, verified app build. Pushes and pull requests do not run GitHub Actions; the optional hosted workflow is manual-only. Use `just lint` or `just test` for a shorter development loop, and `just --list` to discover the other commands.

Tests use temporary queues, fake Codex responses, loopback HTTP, and Unix sockets. They do not enroll or dispatch real chats. Allow local sockets in a sandbox. Native pagination tests compile a temporary Swift executable; the packaged acceptance check uses an isolated home.

## Component map

| Component | Responsibility |
| --- | --- |
| `resume_queue.py` | Queue persistence, enrollment anchors, quota eligibility, and uncertain-send recovery |
| `codex_bridge.py` | Read-only app-server calls and desktop-owned continuation over IPC |
| `tokenmaxxer.py` | CLI and scheduled worker |
| `tokenmaxxer_ui.py` | Loopback HTTP API for the native card |
| `menubar/` | Native AppKit menu, chat picker, setup, and lifecycle |
| `app_runtime.py`, `app_lifecycle.py`, `bundle_install.py` | Runtime paths, login services, and installation |
| `app_update.py`, `menubar/AppUpdate.swift` | GitHub release checks, verified staging, atomic replacement, and restart |
| `build_bundle.py`, `release.py`, `verify_bundle.py` | Release metadata, packaging, and archive acceptance |
| `disk_image.py`, `assets/DMGBackground.swift` | Drag-to-Applications disk image, installer artwork, and mounted-image verification |

Keep queue mutations behind `locked_queue`. Persist sending state before dispatch and never turn a missing reply into an automatic retry. The CLI and native card share that contract. Preserve installed service identities and data paths during branding changes. More detail is in the [feature contracts](features/index.md).

## Changes

Use focused branches and Conventional Commits, and sign commits. Add behavior tests for changed contracts and update the relevant guide in the same change. Avoid installing development builds over a working installation simply to run tests: source and bundled installations share login service labels.

Open a pull request against `master` with the problem, resulting behavior, and verification performed. Run `just ci` locally before pushing and include the result in the pull request; no hosted check runs automatically. The release builder rejects incompatible Python runtimes through its Mach-O audit.

Never commit queue files, logs, settings, credentials, local verification records, or build environments. Use synthetic chat data in fixtures and screenshots. Bug reports should describe a minimal reproduction without private chat contents.

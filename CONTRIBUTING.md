# Contributing

Use an Apple Silicon Mac with Apple's command-line tools, just 1.58 or later, uv, Node.js, and Go. Python dependencies, including the official Codex SDK, are pinned in `requirements.txt`; packaging dependencies are pinned in `requirements-build.txt`. See [build setup](RELEASING.md#toolchain).

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
| `resume_queue.py` | Queue persistence, enrollment anchors, quota eligibility, and uncertain-send recovery |
| `codex_bridge.py` | Bounded Codex SDK reads and desktop-owned continuation over IPC |
| `carrie_on.py` | CLI and scheduled worker |
| `carrie_on_ui.py` | Loopback HTTP API for the native card |
| `menubar/` | Native AppKit menu, chat picker, setup, and lifecycle |
| `app_runtime.py`, `app_lifecycle.py`, `bundle_install.py` | Runtime paths, login services, and installation |
| `legacy_migration.py` | Import of older bundled installations, recovery copies, and old service removal |
| `app_update.py`, `menubar/AppUpdate.swift` | GitHub release checks, verified staging, atomic replacement, and restart |
| `build_bundle.py`, `release.py`, `verify_bundle.py` | Release metadata, packaging, and archive acceptance |
| `disk_image.py`, `assets/DMGBackground.swift` | Drag-to-Applications disk image, installer artwork, and mounted-image verification |

Keep queue mutations behind `locked_queue`. Persist sending state before dispatch and never turn a missing reply into an automatic retry. The CLI and native card share that contract. Changes to installed service identities and data paths require explicit migration guidance. More detail is in the [feature contracts](features/index.md).

## Changes

Use focused branches and Conventional Commits, and sign commits. Add behavior tests for changed contracts and update the relevant guide in the same change. For interactive testing, run `just dev` and install **Carrie On Dev.app** alongside the release app. Dev has separate data and services. Source installations share service labels with the release app. See [local development app instructions](RELEASING.md#local-development-app).

Open a pull request against `master` with the problem, resulting behavior, and test results. Run `just ci` locally before pushing and include the result in the pull request. No hosted check runs automatically. The release builder rejects incompatible Python runtimes through its Mach-O checks.

Never commit queue files, logs, settings, credentials, local verification records, or build environments. Use synthetic chat data in fixtures and screenshots. Bug reports should describe a minimal reproduction without private chat contents.

# Contributing

Use macOS with Apple's command-line tools (`xcode-select --install`) and Python 3.10 or newer. The application and Python tests use only the standard library. Building the standalone app additionally requires the pinned dependencies in `requirements-build.txt`; see [Releasing](RELEASING.md).

Clone the repository and run the tests before changing behavior:

```sh
git clone https://github.com/nickcorin/carrie-on.git
cd carrie-on
python3 -m unittest discover -v
node --check web/app.js
```

Node is only needed for the JavaScript syntax check. Tests use temporary queues, fake Codex responses, loopback HTTP, and Unix sockets. They do not enroll or dispatch real chats. Allow local sockets in a sandbox. Native self-tests run as part of `build_bundle.py`; that command also verifies the actual packaged app in an isolated home.

## Component map

| Component | Responsibility |
| --- | --- |
| `resume_queue.py` | Queue persistence, enrollment anchors, quota eligibility, and uncertain-send recovery |
| `codex_bridge.py` | Read-only app-server calls and desktop-owned continuation over IPC |
| `tokenmaxxer.py` | CLI and scheduled worker |
| `tokenmaxxer_ui.py`, `web/` | Loopback HTTP API and browser dashboard |
| `menubar/` | Native AppKit menu, chat picker, setup, and lifecycle |
| `app_runtime.py`, `app_lifecycle.py`, `bundle_install.py` | Runtime paths, login services, and installation |
| `build_bundle.py`, `release.py`, `verify_bundle.py` | Release metadata, packaging, and archive acceptance |

Keep queue mutations behind `locked_queue`. Persist sending state before dispatch and never turn a missing reply into an automatic retry. The CLI, dashboard, and menu share that contract. Preserve installed service identities and data paths during branding changes. More detail is in the [feature contracts](features/index.md).

## Changes

Use focused branches and Conventional Commits, and sign commits. Add behavior tests for changed contracts and update the relevant guide in the same change. Avoid installing development builds over a working installation simply to run tests: source and bundled installations share login service labels.

Open a pull request against `master` with the problem, resulting behavior, and verification performed. CI runs the Python suite, checks dashboard JavaScript syntax, and builds and verifies an Apple Silicon bundle. The release builder rejects incompatible Python runtimes through its Mach-O audit.

Never commit queue files, logs, settings, credentials, local verification records, or build environments. Use synthetic chat data in fixtures and screenshots. Bug reports should describe a minimal reproduction without private chat contents.

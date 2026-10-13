# Install Carrie On on another Mac

Carrie On ships as a single **Carrie On.app** containing its native menu interface, Python, worker, and local dashboard. The destination needs no Python, Xcode, or runtime download. It does need the Codex desktop app and a signed-in Codex CLI. Keep Codex open and the Mac awake and connected for automatic continuation.

## Install

1. Extract the release ZIP. The `arm64` release targets **Apple Silicon and macOS 13 or later**. Runtime testing on macOS 13 is still outstanding; the current release has been exercised on macOS 27.0.1.
2. Drag **Carrie On.app** into `/Applications` or your home folder's `Applications` folder. It must be directly inside either folder. Replace an older Carrie On.app when updating.
3. Open **Carrie On**. First launch checks for the Codex CLI, saves its location, and sets up the worker, dashboard, and menu login services. If Codex cannot be found, choose its executable in the setup dialog. `command -v codex` in Terminal shows the path on a configured Mac.
4. Open the menu bar item and queue a local chat. The browser dashboard is also available at [localhost:8766](http://127.0.0.1:8766).

The app is **ad-hoc signed, not notarized**. A downloaded copy may be blocked. After attempting to open it, approve that specific app in **System Settings → Privacy & Security → Open Anyway**, then open it again. Only approve a release you trust. No Apple developer membership is needed for this distribution, but it cannot offer the warning-free first launch of a Developer ID signed and notarized app. Managed Macs may prohibit this approval.

Setup runs as your login user, without administrator privileges. Settings, queue/history, logs, and setup records live in `~/Library/Application Support/Tokenmaxxer`. Nothing is written into the signed app. No builder credentials, chats, queue entries, or logs enter the release archive.

## Updates

Choose **Quit Carrie On**, replace **Carrie On.app** with the new release, and open it again. macOS may request approval for the new build. The app detects the changed build and restarts its worker and dashboard from the replacement app. It preserves the saved CLI path, port, interval, socket, Codex home, queue/history, and launch-at-login preference. Reopening the same build leaves healthy services running. Quit stops the app and its background services; reopening restarts them. The settings menu's **Launch at Login** setting controls automatic startup independently of manual launches. See [startup and quit](features/app-lifecycle.md).

When upgrading from Tokenmaxxer, quit the old app before opening Carrie On. After Carrie On launches successfully, remove the old Tokenmaxxer.app to avoid opening it accidentally. The bundle identifier, login service labels, backend executable, and `~/Library/Application Support/Tokenmaxxer` remain stable, so saved settings and queues carry forward without moving data.

Updates are distributed as versioned ZIP files; there is no automatic update downloader. Keep the previous ZIP if you need to restore its app. Back up your Application Support folder before downgrading across a release with a documented data-format migration.

A pre-0.4 bundled installation already uses the same data directory, so its queue and logs remain available. Setup imports its connection settings and replaces its services. After successful migration, the old `Tokenmaxxer Menu.app` and `~/Library/Application Support/Tokenmaxxer/app` runtime can be removed manually. Source installations use the same service labels, but their project-local queue is not imported. Finish or cancel old source enrollments deliberately before switching.

## Configuration and recovery

The bundled backend is `Carrie On.app/Contents/MacOS/tokenmaxxer`. These examples assume the app is in your home Applications folder; substitute `/Applications` if installed there.

```sh
"$HOME/Applications/Carrie On.app/Contents/MacOS/tokenmaxxer" setup --port 8877 --interval 90
"$HOME/Applications/Carrie On.app/Contents/MacOS/tokenmaxxer" setup --codex /absolute/path/to/codex
"$HOME/Applications/Carrie On.app/Contents/MacOS/tokenmaxxer" setup --socket /absolute/path/to/ipc.sock --codex-home /absolute/path/to/codex-home
"$HOME/Applications/Carrie On.app/Contents/MacOS/tokenmaxxer" doctor
"$HOME/Applications/Carrie On.app/Contents/MacOS/tokenmaxxer" list --history
```

Setup options update only the specified settings; reopen the menu after changing its port. Settings are stored in `settings.json`. To clear a custom socket or Codex home, set its value to `null` there and reopen. `CODEX_HOME` is captured from the environment only when the settings do not yet contain a Codex home choice. Later launches preserve the saved choice, including `null` for the default, regardless of their environment. `doctor` checks live connectivity without starting a model turn; setup checks that the CLI executes but does not validate sign-in or desktop connectivity.

If setup is interrupted, reopen the app to retry. Service registration is not transactional; a failure can leave services stopped until the next successful setup. Setup is protected by a per-user lock. **Retry** in the error dialog repairs the worker and dashboard. For an occupied port, run setup with a free port and reopen.

Logs are in `~/Library/Application Support/Tokenmaxxer/.runtime/`. The login jobs are `com.nickcorin.tokenmaxxer`, `com.nickcorin.tokenmaxxer.ui`, and `com.nickcorin.tokenmaxxer.menu`. macOS Background Items settings must allow these services. Advanced repair is available through `setup --repair`.

To uninstall, remove the login services before deleting the app:

```sh
"$HOME/Applications/Carrie On.app/Contents/MacOS/tokenmaxxer" remove-installation --yes
```

Then quit Carrie On and move it to Trash. Settings, queue/history, and logs remain in Application Support; delete that directory manually only if you also want to discard personal data. The source CLI's `uninstall` command removes only the worker.

## Build and release

See [Releasing](RELEASING.md) for the pinned build environment, local builds, checksums, GitHub CI, and draft releases.

## Release verification

To rerun the packaged acceptance check:

```sh
.venv/bin/python verify_bundle.py dist/Carrie-On-0.5.1-macos-arm64.zip
```

It extracts into paths containing spaces, copies the app to an isolated home, removes Python from PATH, and invokes the native first-launch helper with `--no-start`. It checks service paths, persisted configuration, queue operations, dashboard assets, release health, app immutability, and code signatures. Its fake Codex executable only answers the setup version check. It never loads login services or dispatches a chat. `--no-start` is restricted to fresh test installations.

Unit tests exercise repeated setup, upgrades, preservation of settings and queue/history, recovery after partial registration, and migration of legacy connection settings. Before broader distribution, manually test Gatekeeper approval, real first launch, login startup, `doctor`, update, and removal on another Mac, including macOS 13 if claiming that OS as supported. Local tests on macOS 27.0.1 and deployment-target audits do not establish that compatibility.

## Command reference

`tokenmaxxer setup [--codex PATH] [--socket PATH] [--codex-home PATH] [--port PORT] [--interval SECONDS] [--repair] [--no-start] [--json]` reconciles per-user services and persists settings. First-install defaults are port 8766 (1024–65535) and interval 60 seconds (minimum 30). The bundled queue location is fixed; worker commands still accept `--state PATH` for inspecting other queues.

`tokenmaxxer remove-installation --yes` stops and removes all three login services, retaining the app and personal data. Setup commands return 0 on success, 1 for environment errors, 2 for argument errors, and 130 for interruption. `--json` returns structured setup success or failure on stdout; ordinary failures use stderr. Worker `--version` and the app's Info.plist report `VERSION`.

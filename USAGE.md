# Carrie On usage guide

Queue an instruction for a local Codex chat after a usage reset. The FastAPI backend checks quota metadata and asks the desktop app to continue eligible work. Polling and queue management require no model inference; only resumed work consumes usage.

Use the bundled app on an Apple Silicon Mac with the Codex desktop app and a signed-in Codex CLI. Keep the Mac awake and connected. Source development needs Python 3.10 or later and `just setup`. See [installation](INSTALL.md) and [architecture](ARCHITECTURE.md).

## Menu bar

**Carrie On** runs in the macOS menu bar and, by default, starts at login. Its monochrome C-arrow logo, queued-chat count, and next-reset countdown form a single clickable item: the mark followed by `3 · 2h 14m`, for example. Longer countdowns use days and hours; shorter ones use minutes, with `<1m` in the final minute. Clicking anywhere on the item opens the chat picker directly below it. A simple progress bar shows the remaining quota in the most constrained usage window. Its color blends continuously from red at 0% through amber at 50% to green at 100%, with smooth color and fill transitions as usage updates; hover for individual usage windows and their reset times. Hover over the menu bar item for the exact next reset. With several quota windows, the next reset may precede the reset required to resume a particular chat; the native card shows each enrollment's deadline.

It refreshes automatically every minute, on wake, and when opened. The countdown also advances locally before each background fetch. The popover updates while visible and has no manual refresh action. A dash in place of the count or countdown means that information is unavailable; `due` means all reported reset times have passed and fresh data is needed. The settings menu contains **Launch at Login** and **Quit Carrie On**. Quit stops the menu app, backend, including its dispatch worker while preserving the queue. Reopening the app starts them again. Launch at Login controls automatic startup of all components without stopping the current session; upgrades preserve the choice. See [startup and quit](features/app-lifecycle.md) for details.

**Queued** and **Chats** collapse independently and remember their expanded state. Each heading has a separator extending to the right edge, and its entire row toggles the section. Each list has its own scroll area. Search stays above both sections and filters chat titles in both as you type; section counts reflect matching results. Clearing search restores both lists without changing queue membership. Compact cards show the project and relative last activity, ordered newest first within each section; unknown times sort last. Hover over the activity for its exact time. A compact caption row above the bar labels it **Usage remaining** and shows the live reset countdown. Click an available card to flip it over and focus its instruction editor. Press **Enter** or click the compact send-arrow **Add to queue** icon inside the editor to enqueue it and return to the list. **Shift–Enter** inserts a new line; **Escape** returns without submitting. Clicking the title or blank card body flips it back without queueing. There are no selection checkboxes or batch queue button. An empty editor uses its placeholder, **Please continue with your last task.**, as the submitted instruction. Draft instructions persist across searches while the app runs. Card flips respect Reduce Motion.

Queued chats show plain saved instructions with an amber clock beside the title. Sending uses a blue spinner; failures use a red warning icon. Hover the state icon for its explanation or failure reason. Amber styling and placement in the **Queued** section distinguish them from available chats. Hovering over a queued card narrows its right edge to reveal a trash button for direct removal; moving onto the button keeps it revealed. Click a queued card to flip it vertically, edit the instruction, and press **Enter** or click the **Save instruction** send arrow inside the editor. To remove it, return to the card front and use the trash button revealed on hover. Send, save, and trash controls highlight on hover and press. Clicking its title or blank body returns without saving; reopening restores the saved instruction. Saving preserves reset anchors and rejects conflicting revisions, active deliveries, and finished items. Removal cancels the enrollment, preserves its history, and leaves the chat intact; it cannot recall an instruction already accepted by Codex. Queued metadata refreshes independently of search. Project names are shown when metadata is available; otherwise the card says **Local chat**. If several usage buckets exist, choose a bucket before queueing.

Search, usage, and queue status refresh in the background with a small activity indicator. Each card has its own action state: an in-progress or unconfirmed write blocks only that card's action controls. Other cards, search, scrolling, and flipping remain usable. Hover over a card's warning icon for an error; uncertain writes are checked against the saved queue before retrying. Opening **Carrie On Menu** from Applications also opens the picker; login startup keeps it hidden.

If a queued chat cannot be reactivated, its card turns red and shows the reason. It remains in Queued and can be removed from its hover control. Retryable failures, including quota waits and busy chats, retry after the active attempt expires. A stale entry means the conversation already continued and will not receive this instruction. Errors while adding or editing remain separate from entry state.

The native popover follows the Mac's light or dark appearance and accent color. AppKit supplies the popover material, including Liquid Glass where supported; compact action controls sit inside the instruction editors. Native controls honor system accessibility preferences. See Apple's [Liquid Glass adoption guidance](https://developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass).

Install or rebuild it with `python -m tools.install_menubar` after `just setup` (requires Apple's Swift command-line tools). For a nondefault local API port, add `--port PORT`. This creates `~/Applications/Carrie On Menu.app` and the `com.nickcorin.carrieon.menu` login service. Background polling reads only a compact local summary, with no chat titles, prompts, or account identifiers. Opening the picker reads chat titles and queue state locally; the card's queue icon enrolls the chat through the same validated commands as the local API. The app never runs inference. The native card uses a local API service, which must be running. Its menu uses Apple's [NSStatusItem API](https://developer.apple.com/documentation/appkit/nsstatusitem).

Source installations offer **Settings → Rebuild and Restart…**. After confirmation, Carrie On runs the same `python -m tools.install_menubar` builder with `--prepare-app`, using the checkout, Python interpreter, and API port saved when the menu app was installed. The progress window streams compiler, self-test, and signing output. Only a successful build replaces the app and restarts its services; build failures leave the current app running and keep diagnostics visible for copying. Local edits are included; the action does not pull or switch Git branches. Queue, settings, and launch-at-login preferences are preserved. If the checkout or interpreter moves, reinstall from its new location. Older source apps need a terminal rebuild once to gain this action and its saved build paths.

**Carrie On Dev.app** checks for newer commits in its saved checkout and offers **Update to &lt;short hash&gt;** when available. That action rebuilds the entire app. See [local development app](RELEASING.md#local-development-app) for build requirements and first installation.

To remove the menu app, quit it, run `launchctl bootout gui/$(id -u)/com.nickcorin.carrieon.menu`, and remove `~/Library/LaunchAgents/com.nickcorin.carrieon.menu.plist` and `~/Applications/Carrie On Menu.app`. Logs are in `.runtime/menubar.log` and `.runtime/menubar-error.log`. The installed login service also writes a private `.runtime/menu-status.json` snapshot of status-item visibility, popover geometry, appearance, and data availability for troubleshooting; it never includes chat contents.

## Terminal commands

After `just setup`, activate `.venv` or use `.venv/bin/carrie-on`. The bundled executable is `Carrie On.app/Contents/MacOS/carrie_on`. Both clients talk to the same local service as the menu.

```sh
carrie-on status
carrie-on doctor
carrie-on chats list --search 'Chat title'
carrie-on queue add 'Exact chat title'
carrie-on queue add 'Another chat' --instruction 'Finish the implementation and run the tests.'
printf '%s\n' 'Finish the review.' | carrie-on queue add 'Review chat' --instruction-file -
carrie-on --json queue list
carrie-on queue show UUID
carrie-on queue edit UUID --instruction-file instruction.txt
carrie-on queue cancel UUID
carrie-on queue list --history
```

Use an exact unique title, UUID, or `codex://threads/UUID` link when adding. The default instruction is “Please continue with your last task.” `--instruction-file -` reads stdin; files and stdin preserve Unicode and newlines. Use `--instruction=--text` when the instruction starts with a dash. Several quota buckets require `--limit-id`, selected using `usage`. Remote hosts and ChatGPT conversations are unsupported.

Global options precede the command: `--profile release|dev`, `--server http://127.0.0.1:PORT`, `--timeout SECONDS`, and `--json`. Their environment equivalents are `CARRIE_ON_PROFILE`, `CARRIE_ON_SERVER`, and `CARRIE_ON_TIMEOUT`. Explicit options win. Otherwise the client reads the selected profile's saved port, then its default (8766 or 8767). The server URL must be loopback HTTP. `--help`, nested command help, shell completion, and `--version` are available.

JSON results go to stdout and errors to stderr. Exit codes are 0 for success, 1 for local environment/file errors, 2 for invalid input, 3 for missing items, 4 for conflicts or stale revisions, 5 for service/transport failures, and 130 for interruption. Commands do not prompt. Creation prints its UUID to stderr before submitting the request. If the reply is lost or the command is interrupted, inspect it with `queue show UUID`, or retry identical input with `queue add CHAT --id UUID`. Reusing that ID cannot create another item. An edit reads the current ETag and refuses to overwrite a subsequent concurrent change.

| Command | Behavior |
| --- | --- |
| `status` / `usage` | Compact application status or raw usage windows |
| `doctor [--chat UUID]` | Read-only desktop and quota diagnostics |
| `chats list [--search TEXT] [--limit N] [--cursor CURSOR]` | Paginated unarchived catalog |
| `queue add CHAT [--id UUID] [--instruction TEXT / --instruction-file PATH] [--limit-id ID]` | Durable, idempotent creation |
| `queue list [--history] [--state STATE] [--limit N] [--cursor CURSOR]` | Visible work or retained history |
| `queue show UUID` | Saved instructions, state, and the last error |
| `queue edit UUID --instruction TEXT` / `queue cancel UUID` | Edit unclaimed work or cancel/dismiss, retaining history |
| `queue check [--limit N] [--cursor CURSOR]` | Read-only readiness, without claims, opening chats, or sending |
| `service status` / `start` / `stop` / `uninstall` | Manage the selected profile's backend, preserving data |
| `service install [--codex PATH] [--port PORT] [--interval SECONDS] [--socket PATH]` | Register a source backend; use bundled `setup` for whole-app configuration |

## Dispatch and recovery

Each queue item authorizes an instruction after its recorded usage reset. Enrollment anchors the task, model, account, and quota bucket. Busy work, approval/input, and blocked Codex goals are deferred. Task/model changes, completed busy work, and unrelated manual stops prevent sending. Changed or failed items remain visible for inspection and removal. There is no queue pause control; Quit or `service stop` stops processing while preserving state.

A worker acquires the next queued or failed entry without a live attempt, ordered by enqueue time and UUID. Every attempt fetches fresh usage, including before the original reset deadline, so an early reset is detected. Every recorded reset window must advance to a future time and all reported windows need headroom. Empty queues make no Codex calls.

Attempts have expiring leases. If the worker dies, a later iteration reclaims the item. Before sending, it checks fresh conversation state: if the message was accepted already it records success; if the conversation advanced it marks the entry stale and does not retry. Temporary failures and uncertain send replies retry through these checks. The external send and SQLite commit cannot be atomic, so duplicate suppression cannot guarantee exactly-once delivery.

The backend owns `queue.sqlite3`; clients never access it directly. Setup stops the retired JSON worker, imports existing JSON transactionally, and preserves the source file. Import ignores the old pause setting. See [queue behavior](features/queue.md), [migration](ARCHITECTURE.md#installation-and-migration), and the [feature verification matrix](features/verification.md).

For an isolated source server, use a temporary directory and free port:

```sh
carrie-on-server --state /tmp/carrie-test/queue.sqlite3 --port 8877 serve
carrie-on --server http://127.0.0.1:8877 status
```

The server accepts `--codex`, `--socket`, and `--interval` before `serve`. A direct source server defaults to the checkout’s `queue.sqlite3` and adjacent `settings.json`; its logs go to the terminal. With `--state PATH`, it reads settings beside that database.

`carrie-on service install` registers a backend using `~/Library/Application Support/Carrie On/queue.sqlite3`, with `settings.json` and `.runtime/server.log` / `.runtime/server-error.log` in that directory. It shares the release app’s data and services. `carrie-on --profile dev service install` instead uses `~/Library/Application Support/Carrie On Dev` and Dev service labels. Registration preserves saved settings, validates them before publication, and saves relative socket paths as absolute paths. Release source registration imports the stopped checkout’s legacy JSON queue once; Dev registration does not import that queue. See [source migration and recovery](INSTALL.md#update-a-source-installation) before switching between these modes. Prefer `just dev` for interactive development.

## Verification

Run `just test` for isolated domain, SQLite, HTTP, CLI, IPC, native, and installer checks. `just dev` builds and verifies the complete Dev bundle. `doctor` checks live read compatibility without inference. Synthetic resets do not prove behavior at a future real quota reset. A real send requires an explicitly authorized disposable chat; see [release verification](INSTALL.md#release-verification).

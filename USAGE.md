# Carrie On usage guide

Carrie On is a deliberate spelling of “carry on.” Its cobalt app icon and monochrome menu-bar mark pair a C with a forward arrow. See [brand assets](assets/README.md) for artwork and regeneration instructions.

Service identifiers, command filenames, and the Application Support directory use the Carrie On identity. See [upgrade instructions](INSTALL.md#updates) for installations with an older identity.

Resume selected local Codex chats after a usage reset. A Python program checks quota metadata and asks the running desktop app to continue eligible chats. Polling, queue management, and the resume trigger require no model inference. Only the resumed work consumes model usage.

Requires macOS, Python 3.10+, a signed-in Codex CLI, and the desktop app. Keep the Mac awake and connected. The internal desktop IPC protocol was inspected on **26.1007.30912 (20156)**; live reads were tested with CLI **0.161.0**. This adapter is version-sensitive: unsupported responses stop dispatch rather than falling back to a separate agent process.

For another laptop, use the [single-app Mac distribution](INSTALL.md). Drag **Carrie On.app** into Applications and open it; Python is bundled and first launch sets up the login services. To update, quit the menu, replace the app, and reopen it. Settings and queue/history remain outside the signed bundle. Releases use `VERSION`, `CHANGELOG.md`, and a single `build_bundle.py` command that tests, signs, and verifies the archive. The instructions below describe running and installing from source; bundled installations use the executable and data paths in the installation guide.

## Menu bar

**Carrie On** runs in the macOS menu bar and, by default, starts at login. Its monochrome C-arrow logo, queued-chat count, and next-reset countdown form a single clickable item: the mark followed by `3 · 2h 14m`, for example. Longer countdowns use days and hours; shorter ones use minutes, with `<1m` in the final minute. Clicking anywhere on the item opens the chat picker directly below it. A simple progress bar shows the remaining quota in the most constrained usage window. Its color blends continuously from red at 0% through amber at 50% to green at 100%, with smooth color and fill transitions as usage updates; hover for individual usage windows and their reset times. Hover over the menu bar item for the exact next reset. With several quota windows, the next reset may precede the reset required to resume a particular chat; the native card shows each enrollment's deadline.

It refreshes automatically every minute, on wake, and when opened. The countdown also advances locally before each background fetch. The popover updates while visible and has no manual refresh action. A dash in place of the count or countdown means that information is unavailable; `due` means all reported reset times have passed and fresh data is needed. The settings menu contains **Launch at Login** and **Quit Carrie On**. Quit stops the menu app, worker, and local interface while preserving the queue. Reopening the app starts them again. Launch at Login controls automatic startup of all components without stopping the current session; upgrades preserve the choice. See [startup and quit](features/app-lifecycle.md) for details.

**Queued** and **Chats** collapse independently and remember their expanded state. Each heading has a separator extending to the right edge, and its entire row toggles the section. Each list has its own scroll area. Search stays above both sections and filters chat titles in both as you type; section counts reflect matching results. Clearing search restores both lists without changing queue membership. Compact cards show the project and relative last activity, ordered newest first within each section; unknown times sort last. Hover over the activity for its exact time. A compact caption row above the bar labels it **Usage remaining** and shows the live reset countdown. Click an available card to flip it over and focus its instruction editor. Press **Enter** or click the compact send-arrow **Add to queue** icon inside the editor to enqueue it and return to the list. **Shift–Enter** inserts a new line; **Escape** returns without submitting. Clicking the title or blank card body flips it back without queueing. There are no selection checkboxes or batch queue button. An empty editor uses its placeholder, **Please continue with your last task.**, as the submitted instruction. Draft instructions persist across searches while the app runs. Card flips respect Reduce Motion.

Queued chats show plain saved instructions with an amber clock beside the title. Sending uses a blue spinner; failures use a red warning icon. Hover the state icon for its explanation or failure reason. Amber styling and placement in the **Queued** section distinguish them from available chats. Hovering over a queued card narrows its right edge to reveal a trash button for direct removal; moving onto the button keeps it revealed. Click a queued card to flip it vertically, edit the instruction, and press **Enter** or click the **Save instruction** send arrow inside the editor. To remove it, return to the card front and use the trash button revealed on hover. Send, save, and trash controls highlight on hover and press. Clicking its title or blank body returns without saving; reopening restores the saved instruction. Saving preserves the reset deadline and rejects conflicting edits or entries already sending or requiring attention. Removal cancels the enrollment, preserves its history, and leaves the chat intact; it cannot recall a continuation already accepted by Codex. Queued metadata refreshes independently of search. Project names are shown when metadata is available; otherwise the card says **Local chat**. If several usage buckets exist, choose a bucket before queueing.

Search, usage, and queue status refresh in the background with a small activity indicator. Each card has its own action state: an in-progress or unconfirmed write blocks only that card's action controls. Other cards, search, scrolling, and flipping remain usable. Hover over a card's warning icon for an error; uncertain writes are checked against the saved queue before retrying. A paused queue is clearly marked. Opening **Carrie On Menu** from Applications also opens the picker; login startup keeps it hidden.

If a queued chat cannot be reactivated, its card turns red and shows the reason. It remains in Queued and can be removed from its hover control. Temporary failures retry automatically; uncertain delivery is labeled **Resume unconfirmed** and requires inspection before re-queueing. Ordinary waiting and errors while adding or editing a card do not use this failed state.

The native popover follows the Mac's light or dark appearance and accent color. AppKit supplies the popover material, including Liquid Glass where supported; compact action controls sit inside the instruction editors. Native controls honor system accessibility preferences. See Apple's [Liquid Glass adoption guidance](https://developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass).

Install or rebuild it with `python3 install_menubar.py` (requires Apple's Swift command-line tools). For a nondefault local API port, add `--port PORT`. This creates `~/Applications/Carrie On Menu.app` and the `com.nickcorin.carrieon.menu` login service. Background polling reads only a compact local summary, with no chat titles, prompts, or account identifiers. Opening the picker reads chat titles and queue state locally; the card's queue icon enrolls the chat through the same validated commands as the local API. The app never runs inference. The native card uses a local API service, which must be running. Its menu uses Apple's [NSStatusItem API](https://developer.apple.com/documentation/appkit/nsstatusitem).

To remove the menu app, quit it, run `launchctl bootout gui/$(id -u)/com.nickcorin.carrieon.menu`, and remove `~/Library/LaunchAgents/com.nickcorin.carrieon.menu.plist` and `~/Applications/Carrie On Menu.app`. Logs are in `.runtime/menubar.log` and `.runtime/menubar-error.log`. The installed login service also writes a private `.runtime/menu-status.json` snapshot of status-item visibility, popover geometry, appearance, and data availability for troubleshooting; it never includes chat contents.

## Use the interface

Open **Carrie On** from Applications and click its menu bar item. Search for a local chat, open its card, optionally edit the continuation instruction, and use **Add to queue**. Each enrollment authorizes a single continuation after reset. Queued cards show their saved instructions and status; remove an enrollment with the trash control on the card front. Keep this Mac awake and Codex open for automatic continuation.

The card uses a local API service listening only on `127.0.0.1`; the worker owns polling and dispatch. For source installations, run `python3 carrie_on_ui.py install` to register the API service, then install the native menu app with `python3 install_menubar.py`. The API installer accepts `--state`, `--codex`, `--socket`, and `--port` before `install`. Logs are in `.runtime/dashboard.log` and `.runtime/dashboard-error.log`.

Manual component removal must also remove the matching retained service definition under `~/Library/Application Support/Carrie On/Lifecycle/Agents/`. See [startup and quit](features/app-lifecycle.md) for registration and removal semantics.

## Terminal commands

Run commands from this directory:

```sh
python3 carrie_on.py doctor
python3 carrie_on.py install
python3 carrie_on.py threads "Chat title"
python3 carrie_on.py queue "Chat title"
python3 carrie_on.py list
```

Use an exact chat title, UUID, or `codex://threads/UUID` link. Ambiguous titles require an ID. Enrollment authorizes a single continuation. The default instruction is “Continue the unfinished work already requested in this chat.” For a specific instruction:

```sh
python3 carrie_on.py queue "Chat title" --prompt "Finish the implementation and run the tests."
python3 carrie_on.py queue "Another chat" --prompt-file continuation.txt
printf '%s\n' 'Finish the review and report findings.' | python3 carrie_on.py queue "Review chat" --prompt-file -
```

These commands work while model usage is exhausted. You can also ask an agent to run them when model access is available. Remote hosts and ChatGPT conversations are unsupported. Multiple quota buckets require `--limit-id`, chosen using `usage`.

## Behavior

Enrollment captures the intended task, instruction, account, and reset baseline; chat readiness is checked at resume time. The interface reuses validated usage data up to 60 seconds old when its reset timestamps are still in the future, avoiding another helper startup in the usual enqueue path. A live task snapshot remains necessary to anchor the request to the current work. Slow enrollment reads do not hold the queue lock. Chats awaiting approval or with paused goals may be queued, but cannot resume while blocked. See the [enrollment and resume contract](features/continuation-queue.md) for the data split and fallback behavior.

The per-user `launchd` job checks every 60 seconds while Carrie On is running. Automatic login startup follows the Launch at Login setting. It makes no network calls with an empty or paused queue. Waiting enrollments fetch fresh usage on every check, including before the recorded reset deadline, so an early manual reset can resume work promptly. The relevant reset timestamps must advance to future times and every reported window in the selected bucket must have headroom. It never buys credits, redeems resets, changes models, or alters permissions.

The app retains ownership of resumed chats and supplies their existing model, tools, directory, and permission settings. A dormant queued chat can be reopened through its ordinary deep link; this may change the visible chat. The monitor never starts a competing app-server turn. Its app-server client only allows read methods.

Active chats and chats awaiting approval/input are deferred. A new turn or model change after enrollment holds the entry for review. Work enrolled while running is removed if it finishes normally before reset; interruption or unrelated failure requires review. A chat deliberately enrolled while idle receives its continuation after reset even if its previous turn completed normally. Paused or budget-limited goals are never resumed automatically.

Successful dispatch removes the enrollment and retains history. “Resumed” means the desktop accepted the continuation, not that the work completed. Re-enroll for another reset.

## Commands and configuration

Global options precede the command. `--state PATH` selects the queue (default: `resume-queue.json` beside the program); `--codex PATH` selects the CLI (default: PATH lookup); `--socket PATH` selects desktop IPC (default: `CODEX_HOME/ipc/ipc.sock`, or `~/.codex/ipc/ipc.sock`); `--json` emits machine-readable output. The installer captures absolute paths and an explicitly set `CODEX_HOME`. There are no additional configuration sources or API keys.

| Command | Behavior |
| --- | --- |
| `doctor [--thread UUID]` | Read live quota and check desktop connectivity; optionally inspect a loaded chat. Starts no model turn. |
| `threads [SEARCH]` | List up to 100 recent unarchived local chats matching a search. |
| `usage` | Read account and usage-window metadata. |
| `queue CHAT [--prompt TEXT / --prompt-file PATH] [--limit-id ID]` | Enroll once; duplicates are rejected. `-` reads the prompt from stdin. |
| `list [--history]` | Show entries, last check/error, and optional history. |
| `remove CHAT` | Cancel an exact queued title, chat ID, or enrollment ID. Preserves chat and history. |
| `pause` / `resume` | Disable/enable dispatch without clearing entries. Does not re-arm uncertain sends. |
| `tick [--dry-run]` | Check once. Dry-run sends nothing, updates no state, and does not open dormant chats. |
| `install [--interval SECONDS]` | Install/replace the launchd job; default 60 seconds, minimum 30. |
| `service` | Show launchd registration and last exit status. |
| `uninstall` | Stop/remove the job, preserving queue and logs. |

Exit status: 0 for completed commands, 1 for input/connection/service errors, 130 for keyboard interruption. A completed check may leave entries waiting or needing attention; inspect `list`. An unavailable `service` reports `installed: false`.

## Recovery

Queue writes use a process lock, atomic replacement, and disk flushes. A durable `sending` state precedes dispatch. After a crash or lost reply, the next check looks for the enrollment's message ID in desktop history. Confirmed deliveries are removed; unresolved deliveries become `needs_attention` and are never blindly resent. This avoids duplicates under uncertain delivery; it does not promise distributed exactly-once execution.

Unavailable usage, a closed app, and network errors leave entries for another check. Missing/invalid quota data, account changes, changed chats, and incompatible history shapes require review. The monitor does not infer completion from prose or bypass approvals. A small race remains between reading live state and sending; the desktop is the final authority on accepting a turn.

```sh
python3 carrie_on.py doctor
python3 carrie_on.py list --history
python3 carrie_on.py service
python3 carrie_on.py tick --dry-run
```

Logs live in `.runtime/monitor.log` and `.runtime/monitor-error.log`. The job is `com.nickcorin.carrieon` in `~/Library/LaunchAgents/`. If Python, Codex, or this project moves, reinstall. After a desktop update, run `doctor --thread UUID` before relying on unattended dispatch.

For `needs_attention`, inspect the actual chat first. If another continuation is still required and was not delivered, remove and re-enroll. This is a new authorization, not an automatic retry. Empty version-1 queues migrate automatically; populated legacy queues require deliberate re-enrollment. Keep the former native scheduled monitor paused or delete it to prevent competing dispatch.

## Verification and implementation

```sh
python3 -m unittest -v
python3 -m pydoc codex_bridge resume_queue app_runtime macos_compatibility
```

Tests cover exhausted/reset quotas, multiple windows, changed accounts/chats, busy or manually stopped work, pauses, corrupt state, and lost replies. `doctor` checks live model-free reads. Synthetic resets do not establish the backend's behavior at a future reset. See [release verification](INSTALL.md#release-verification) for the manual checks and compatibility limits.

`codex_bridge.py` owns protocol adaptation, `resume_queue.py` owns enrollment and durable transitions, and `carrie_on.py` owns CLI and worker installation. `carrie_on_ui.py` adapts local API requests from the native card to those same commands. No third-party packages are required. See the [feature map](features/index.md) and [official App Server reference](https://learn.chatgpt.com/docs/app-server).

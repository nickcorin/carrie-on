# Features

Carrie On queues instructions for local Codex chats. Start with [installation](../../README.md#install). Use the installed menu app for acceptance checks. To use its CLI, run `"/Applications/Carrie On.app/Contents/MacOS/carrie_on" --help`; substitute Carrie On Dev for development acceptance. From a source checkout, use `uv run --locked carrie-on`. Neither path requires activating a virtual environment.

Automated checks run from the checkout with `just test`, or with the explicit `uv run` commands below. They use isolated data and synthetic peers; they do not replace checks through the installed app.

- [Instruction queue](queue.md): enrollment, reset checks, dispatch, and recovery.
- [Distribution](distribution.md): build, transfer, install, upgrade, and remove a self-contained Mac bundle.
- [Menu bar](menu-bar.md): usage, resets, and queue count at a glance.
- [Startup and quit](app-lifecycle.md): launch at login, stop all app processes, and reopen with the queue preserved.
- [Verification matrix](verification.md): retained feature coverage and manual acceptance limits.

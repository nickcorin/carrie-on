# Startup and quit

Open the menu-bar picker and its settings menu. **Launch at Login** controls automatic startup of the menu app, worker, and local interface together. It defaults to enabled for existing installations. Changing it affects future logins, without interrupting the current session. Reinstalling or upgrading preserves the choice.

**Quit Carrie On** unloads the worker and local interface services, then exits the menu app. Monitoring and dispatch stop; the queue and instructions remain saved. Opening Carrie On again starts the services regardless of the login preference. Quitting does not change that preference. A continuation already accepted by Codex continues in Codex; Quit cannot recall it. If service shutdown fails, the app stays open and reports the failure.

Service transitions wait up to roughly ten seconds for launchd's registration state to settle. A successful stop request can leave the service listed briefly while it exits; updates and Quit wait for removal before proceeding. A service that remains loaded beyond that limit produces a timeout instead of replacing the running app.

Startup settings live in `~/Library/Application Support/Carrie On/Lifecycle/preferences.plist`. The same directory retains `Agents/` definitions for manual launch. When startup is disabled, the three app plists are absent from `~/Library/LaunchAgents/`; their currently loaded jobs remain active until Quit. Enabling startup restores those registrations. Installers and the native app share a file lock to prevent conflicting preference changes. Only this app's service labels are managed; their Carrie On identifiers are shared by the installers and native app.

When manually removing a component, also delete its matching plist from `Lifecycle/Agents/`; otherwise a later launch can restore its registration. The worker uninstall and bundled remove-installation commands remove both definitions automatically.

`python3 -m unittest test_app_lifecycle -v` verifies registration, preference preservation across reinstall, invalid settings, and removal. Native self-checks exercise disabling and restoring all registrations, manual launch, idempotent service startup, and shutdown with isolated files and a simulated launchd runner. Manual acceptance: disable startup, confirm the current app still works, quit and verify the worker/interface jobs and menu process stop, then reopen and verify the setting remains disabled and services return. Restore the initial preference after testing. Actual login startup requires a logout/login test.

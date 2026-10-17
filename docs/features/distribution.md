# Distribution

## Sub-features

- Ship a single ad-hoc signed Carrie On.app containing Python, the local API, worker, native menu, and custom app icon.
- Configure backend and menu login services on first launch without development tools.
- Detect app replacements and update service registrations while preserving connection settings, queue, and history.
- Import the v0.5.7 JSON queue into SQLite on first launch and retire its standalone worker.
- Check for a newer stable GitHub release on startup and show **Update to &lt;version&gt;** when available. Download and install it after confirmation.
- Rebuild the entire Carrie On Dev app from its saved checkout with live output, replacing and restarting only after tests, compatibility checks, and signing succeed. Source menu installations reuse their native menu builder.
- Build versioned releases with tests, package integrity checks, checksums, and release manifests.
- Offer a DMG with an illustrated drag-to-Applications window and the same signed app as the updater ZIP.
- Remove login services while preserving personal data.
- Build Carrie On Dev with isolated settings, queue, login services, logs, and port using the same runtime behavior as release.

## Setup and usage

Follow [installation](../../README.md#install) for setup and [releasing](../../README.md#releasing) for publishing. The app must reside directly in `/Applications` or `~/Applications`. Its Apple Silicon build targets macOS 13+, but runtime testing on macOS 13 is still outstanding. Codex desktop and a signed-in Codex CLI remain prerequisites.

First launch finds or asks for the CLI executable, saves settings in Application Support, registers services, and checks the local API before opening the menu. It never modifies the signed app. Fresh installations contain no enrolled chats, logs, or builder credentials. Replacing and reopening the app updates services based on its build identity. Reopening the same build preserves healthy running services.

## Upgrade from v0.5.7

Setup stops the retired writer before importing the v0.5.7 queue. Settings and launch-at-login preferences stay in place. The JSON source remains unchanged. Each entry commits separately; retry skips stored UUIDs without overwriting edits or reactivating completed work. Readable history imports into SQLite, and incomplete historical records remain available in the JSON source. Pause settings are ignored. Import does not send messages.

Recorded acceptance imports as sent. An uncertain send without recorded acceptance becomes pending work and is checked against Codex before another send. Existing SQLite work takes precedence over conflicting JSON enrollment; conflicting pending records import as cancelled history.

Upgrades from earlier app identities and merging multiple installations are unsupported. Carrie On Dev does not import the release installation.

Direct source servers default to checkout-local SQLite; registered source services use the selected profile’s Application Support data. Registering the release source service stops the old writers and imports a private snapshot of checkout-local JSON. Dev source registration does not import the release queue.

## Update verification and recovery

The updater checks the download's size, SHA-256 checksum, compatibility, bundle identity, and code signature before replacement. It trusts this project's GitHub release assets over HTTPS. Checksums detect damaged downloads. Ad-hoc signatures validate bundle integrity but do not authenticate the developer. The updater does not change Gatekeeper settings or request administrator privileges.

Installation exchanges complete bundles atomically and keeps the previous app in a hidden `.Carrie-On-update-*` directory beside the installed app. The new app restarts its worker and local API. It preserves the CLI path, port, interval, socket, Codex home, queue, history, and launch-at-login preference.

## Verification

Dev compares its built commit with the local checkout's `HEAD` on startup. It offers **Update to &lt;short hash&gt;** only when the built commit is an ancestor of `HEAD`. Otherwise it shows **Check for Updates**. `python -m unittest tests.test_source_build tests.test_app_update -v` tests both labels and checkouts with newer, equal, older, unrelated, or missing commits.

`just dev` builds the development profile with the same build checks as release.
`python -m unittest tests.test_build_profiles -v` exercises both
profiles in the same isolated home, including independent queue state, worker
definitions, ports, and startup preferences.

`python -m unittest tests.test_source_build -v` checks streamed build diagnostics, failed builds, staging with saved build paths and a custom port, signing, and replacement after success. The checks use temporary apps without stopping or installing live services.

`python -m tools.build_bundle` runs unit tests, native self-tests, linkage audits, code-signature checks, and DMG integrity checks before writing packages to `dist/`. Tests cover interrupted setup, updates, stable reopening, settings and queue preservation, legacy connection migration, and invalid install locations. Installing and running the packaged app requires the acceptance steps below.

`python -m unittest tests.test_app_update -v` tests startup checks, offline failures, release selection, and download verification. It also replaces temporary apps to test native installation, rollback after a failed launch, and retry. It never replaces the installed app.

`python -m unittest tests.test_service_installation tests.test_cli -v` checks source commands from outside the checkout, virtual-environment interpreter preservation, saved-setting validation, relative socket reuse, environment isolation, installation locking, publication failure recovery, source JSON import and retry, and interrupted CLI creation with its original UUID.

`python -m unittest tests.test_storage tests.test_composed_service -v` checks JSON import, incomplete history, interrupted imports, and dispatch from raw v0.5.7 entries through the real adapters with controlled peers. The scenarios include unchanged and changed turn anchors, accepted-message reconciliation, and restart persistence.

## Package acceptance

Use Carrie On Dev for local acceptance and a separate Mac or fresh macOS account for release installation checks. Record the package version, macOS version, and Codex version with the results.

1. Build with `just dev` and install Carrie On Dev.app from the DMG in `dist/dev/`. Quit and replace only Dev when testing a new build. Check the app icon and Applications shortcut. First launch must complete setup and open the menu. Open an existing installation again and confirm that its settings and queue remain intact.
2. Run the CLI inside the installed bundle with system tools on PATH. Its version must match `VERSION`. Status must report a healthy worker, diagnostics must reach the selected Codex installation, and the queue must agree with the menu. With the default Dev install location and port:

   ```sh
   app_cli="$HOME/Applications/Carrie On Dev.app/Contents/MacOS/carrie_on"
   PATH=/usr/bin:/bin:/usr/sbin:/sbin "$app_cli" --version
   "$app_cli" --json status
   "$app_cli" --json doctor
   "$app_cli" --json queue list --history
   curl --fail -H 'X-Carrie-On: 1' http://127.0.0.1:8767/openapi.json
   ```

   Adjust the app path or port for a custom installation. The OpenAPI response must contain the queue routes and current app version.
3. Inspect the installed bundle's `Contents/Resources/Documentation` and `Licenses` directories. Guides and their local links must open. Settings, logs, and SQLite files belong in `~/Library/Application Support/Carrie On Dev`, outside the app. Dev's service definitions must use its installed executable and data paths. After setup, `just verify-signature "$HOME/Applications/Carrie On Dev.app"` must still succeed.
4. Follow the [menu checks](menu-bar.md), [queue checks](queue.md#live-dispatch-acceptance), and [startup and quit checks](app-lifecycle.md). Confirm that reopening preserves work and that a successful source rebuild retains the configured port and queue. Check the release installation separately to confirm Dev has not changed its data or services.
5. On the release test Mac or account, install the release DMG and repeat the CLI and lifecycle checks. Test the updater ZIP through the normal update flow, including preserved settings and recovery after failure. Check Gatekeeper approval, login startup, and [removal](../../README.md#uninstall). Include macOS 13 before claiming runtime compatibility with that version.

## Distribution limits

The builder mounts the DMG read-only, checks the Applications shortcut, and copies its app into an isolated directory. It compares the copied app's contents with the signed bundle used to create both packages and verifies its code signature. The release manifest records the DMG's filename, size, and SHA-256 hash. Release checks require both package formats and their checksum files. Built-in updates use the ZIP.

Ad-hoc signing does not provide notarization or remove Gatekeeper approval. Binary deployment targets cannot substitute for runtime tests on the oldest OS.

Bundled setup shares release service labels and Application Support data with a registered release source backend. A direct source SQLite database is not merged automatically. A setup failure can leave services stopped until a retry succeeds. Both CLI service registration and bundled setup hold the same per-profile lock while reading settings, validating configuration, and publishing service definitions.

**Quit Carrie On** stops background jobs and the menu. Reopening restarts them and preserves the launch-at-login preference. Uninstalling requires service removal before deleting the app.

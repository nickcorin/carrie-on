# Changelog

## 0.5.5

- Check for updates once on startup and show Update to the available version; retain Check for Updates when current.
- Let development builds discover newer local checkout commits and rebuild the complete app from the update action.
- Wait for background services to finish shutting down before replacing the app, avoiding false shutdown failures.
- Show the installed version in the settings menu.

- Use the Carrie On identity throughout Python modules, executable names, bundle metadata, login services, local API headers, and Application Support paths, including isolated development builds.
- Reject retired branding in source inputs and packaged application payloads before a release is produced.
- Remove the browser dashboard and bundle only the native menu interface and its local API.
- Preserve the latest queue safeguards, native card states, settings menu, and shared refresh timestamps.
- When upgrading an app with an older identity, install manually. Remove its services and migrate saved settings and queue data as described in the installation guide before opening the new app.

## 0.5.4

- Add a disk image with a drag-to-Applications installer window, alongside the ZIP used by built-in updates.
- Verify that the DMG and ZIP contain the same signed app and publish checksums for both formats.

## 0.5.3

- Add menu-driven GitHub update checks, verified downloads, and Install and Restart with atomic app replacement and recovery copies.
- Simplify chat pagination feedback to subtle loading placeholders while preserving retry and search behavior.

## 0.5.2

- Update the app and dashboard icons with a cobalt and porcelain C-arrow design.
- Generate the app and monochrome tray icons from a shared vector silhouette, preserving the same shape across sizes.
- Verify regenerated artwork against the reference silhouette to prevent shape drift.

## 0.5.1

- Add a justfile for local CI, building, signing, packaging, and draft releases without hosted CI credits.
- Keep GitHub CI manual-only and verify release artifacts against the current clean source commit.
- Add pagination feedback and regression coverage for browsing large chat catalogs.
- Validate release tags against the version and changelog before building.
- Include the MIT license and complete user documentation in release bundles.
- Report the current app version when connecting to Codex.

## 0.5.0

- Rename the app and release bundles to Carrie On, preserving installed service identities and saved data.
- Add a coral C-arrow app icon, a native monochrome tray mark, and matching dashboard branding.

## 0.4.1

- Preserve a saved default Codex home when reopening from a different launch environment, avoiding unnecessary service restarts.

## 0.4.0

- Distribute a single Carrie On.app with Python, the native menu, and dashboard bundled.
- Configure login services on first launch, with native recovery dialogs for missing Codex or setup errors.
- Preserve settings and queue/history across app replacements; refresh services when the build changes.
- Keep application contents immutable after ad-hoc signing.
- Build releases through a single versioned command with automated tests, compatibility checks, archive verification, checksums, and release metadata.

## 0.3.0

- Bundle Python and the native menu for Apple Silicon with a macOS 13 deployment target.
- Install the runtime and separate app launchers through Install.command.

# Changelog

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

- Distribute a single Tokenmaxxer.app with Python, the native menu, and dashboard bundled.
- Configure login services on first launch, with native recovery dialogs for missing Codex or setup errors.
- Preserve settings and queue/history across app replacements; refresh services when the build changes.
- Keep application contents immutable after ad-hoc signing.
- Build releases through a single versioned command with automated tests, compatibility checks, archive verification, checksums, and release metadata.

## 0.3.0

- Bundle Python and the native menu for Apple Silicon with a macOS 13 deployment target.
- Install the runtime and separate app launchers through Install.command.

# Releasing Carrie On

All checks, builds, signing, and releases run locally through the `justfile`. Pushing a branch or tag does not start GitHub Actions. The optional GitHub CI workflow is manual-only and runs the same `just ci` command; do not dispatch it when you want to avoid hosted runner usage.

## Toolchain

Use an Apple Silicon Mac with Apple's Swift command-line tools, just 1.58 or later, uv, Node.js, and Go. GitHub CLI is needed only for uploading and publishing releases. Git must have commit and tag signing configured.

```sh
xcode-select --install
brew install just uv node go gh
just setup
just
```

`.python-version` pins the build interpreter and `requirements-build.txt` pins the packaging dependencies. Ruff and actionlint versions are pinned in the justfile. The first setup and tool runs download dependencies; subsequent checks run on your Mac without GitHub CI credits. uv-managed Python is used because some Homebrew runtimes require a newer macOS version than the app targets.

## Check, build, and package

```sh
just lint
just test
just test-minimum
just ci
```

`just ci` checks Python errors, JavaScript syntax, workflow syntax, release metadata, and Python 3.10 compatibility, then builds and verifies the complete app with the pinned build interpreter. The builder runs the full test suite, native checks, Mach-O compatibility audit, ad-hoc signing, and isolated ZIP acceptance. Tests use synthetic chats and temporary state, without installing login services or dispatching work.

`just build` is an alias for `just package`: compiling, signing, and packaging are a single pipeline so the ZIP always contains the verified app. An existing version is preserved unless replacement is requested. CI deliberately replaces local development artifacts so repeated checks work.

`just branding` checks current tracked and nonignored source files for retired app branding. Packaging also checks bundle paths, file contents, symlink targets, and compressed Python modules before signing; ZIP acceptance repeats the bundle check. Historical Git objects and ignored development caches are outside the source check and are never packaging inputs.

```sh
just package
just package --replace
just package --output '/tmp/Carrie On release'
just verify
just verify '/tmp/Carrie On release/Carrie-On-0.5.1-macos-arm64.zip'
```

Output is a versioned DMG, ZIP, a `.sha256` checksum for each, and a JSON manifest in `dist/`. The DMG provides the drag-to-Applications installation flow; built-in updates continue to use the ZIP. Both packages contain the same signed app. The builder mounts the DMG read-only, checks the Applications shortcut, and verifies a copied-out app against the ZIP payload and its code signature. The manifest records the version, clean source commit (or null for dirty/exported source), build identity, architecture, Python version, compatibility audit, size, and checksum, plus a `disk_image` object with the DMG filename, size, and checksum. Build identities and signing timestamps mean builds are not byte-reproducible. Generated files are ignored by Git.

The built-in updater reads the latest published stable release from GitHub. Keep the `vMAJOR.MINOR.PATCH` tag and generated asset names unchanged, and publish the ZIP and JSON manifest together. Drafts and prereleases are not offered; versions at or below the installed version are never installed. No additional update feed or credentials are required. The release manifest's hash and byte count must describe the final signed ZIP.

For an extracted app, signing and signature checks are also available directly:

```sh
just sign '/tmp/Carrie On.app'
just verify-signature '/tmp/Carrie On.app'
```

Signing is **ad-hoc**, not Developer ID signing or notarization. `just sign` modifies the app, so any existing archive/checksum must be rebuilt afterwards. `just icons` regenerates packaged icon sizes from the master artwork. `just doctor` checks local Codex connectivity without starting a turn.

## Create a release

### Local development app

Dev checks the saved local checkout's `HEAD` once on startup. If the installed commit is behind, the action reads **Update to <short hash>** and uses the local rebuild-and-restart flow. Otherwise it reads **Check for Updates**, which checks again on demand and reports when no update is available. It does not fetch or pull from GitHub. Rebuilding includes the checkout's uncommitted edits.

Run `just dev` to build **Carrie On Dev** into `dist/dev/`. Install its app from
the DMG into Applications alongside Carrie On. Use **Settings → Update to
<short hash>** when an update is available to rebuild from its saved checkout, including the
native interface, bundled Python runtime, and backend. The action runs the same
`build_bundle.py --profile dev` pipeline with `--prepare-app`, streams build
output, and stages a signed replacement only after tests and isolated bundle
acceptance checks succeed. It skips distribution DMG generation. A failure
keeps the current app running and leaves diagnostics visible. Success replaces
the complete app and restarts its dev services, preserving its queue, settings,
and login preference. It does not pull or switch Git branches.

Older dev builds need a fresh `just dev` build installed once to gain this
action and its saved checkout/interpreter paths. Reinstall from a new location
if the checkout or build interpreter moves. No GitHub release or push is needed.

Both profiles use the same optimized native executable, bundled backend, setup
flow, polling interval, dispatch rules, and acceptance checks. The signed build
profile changes installation resources, not feature behavior:

| Resource | Release | Development |
| --- | --- | --- |
| App | Carrie On.app | Carrie On Dev.app |
| Application Support folder | Carrie On | Carrie On Dev |
| Service prefix | com.nickcorin.carrieon | com.nickcorin.carrieon.dev |
| Default local port | 8766 | 8767 |
| Updates | Versioned GitHub releases | Local rebuilds |

Queues, connection settings, login preferences, logs, and installation locks are
independent. Dev starts with an empty queue and the same automatic processing
behavior as release. It does not copy production data. Both apps can connect to
the same live Codex account; use different chats when testing, or quit the other
app before enrolling the same chat. Separate application data does not isolate
the external Codex chats or account quota.

The release commands still build the release profile and reject development
artifacts. Publish versioned GitHub prereleases for beta testing, then mark the
validated release stable; there is no rolling beta feed. The existing in-app
updater offers stable releases only, so beta installation is manual. Dev never
installs a production update over itself.

### Publish a versioned release

Update `VERSION` and add exactly one nonempty `## MAJOR.MINOR.PATCH` section to `CHANGELOG.md`. Commit all source changes with signed Conventional Commits, then push the reviewed commits to `master`. A new published build always gets a new version.

```sh
just release-check
git push origin master
just release
```

`just release` requires a clean checkout, runs all local CI checks, and verifies that the new artifacts match the current commit and version. It then creates a signed `vMAJOR.MINOR.PATCH` tag, pushes that tag to `origin`, and creates a **draft** GitHub release with the changelog notes and all five assets. It does not publish the draft. GitHub CLI resolves the repository from the checkout's remote.

An existing local tag stops `just release` instead of being moved. If the tag was created but its push or draft creation failed, retry without rebuilding or retagging:

```sh
just release-retry
```

If GitHub created the draft but an asset upload was interrupted, use `just upload` to replace its assets. Both recovery commands require artifacts matching the current clean commit and a local tag pointing to that commit. `just upload` rejects published releases. Use `just release-notes` to regenerate `dist/release-notes.md` for review; retries do not silently publish anything.

Download and test the draft on another Mac using the [installation and release verification guide](INSTALL.md#release-verification). Record the actual macOS and Codex versions tested in its notes, including remaining compatibility limits. When satisfied:

```sh
just publish
```

Publishing requires the matching source, tag, artifacts, and an existing draft. Never move a published tag or replace published assets. Correct defects with a new patch version. Recipients can restore the previous app from its ZIP; back up personal data before downgrading across a data-format migration.

## Command behavior

Run `just --list` to discover recipes and `just --dry-run release` to inspect the commands without executing them. Recipe dependencies handle ordering and run once per invocation. Paths are quoted and extra `package` arguments are forwarded unchanged. `just` stops at the first failed command; no later tag push or release creation runs after a failed check.

The underlying `python3 release.py [--tag TAG]` prints Markdown notes. `--check-source` requires a clean Git commit; `--check-assets DIRECTORY` verifies release provenance and integrity; `--check-tag` requires a matching local tag; `--check-draft` requires a GitHub draft. These checks do not mutate source, tags, or releases. File/validation failures return 1 with stderr diagnostics, and argument errors return 2. Without check flags, release metadata works outside a Git checkout. There is no custom credential or environment configuration; Git and GitHub CLI use their existing authentication.

# Releasing Carrie On

Run checks, builds, signing, and releases locally through the `justfile`. Pushing a branch or tag does not start GitHub Actions. To use hosted runners, start the optional GitHub CI workflow manually. It runs the same `just ci` command.

## Toolchain

Use an Apple Silicon Mac with Apple's Swift command-line tools, just 1.58 or later, uv, Node.js, and Go. GitHub CLI is needed only for uploading and publishing releases. Git must have commit and tag signing configured.

```sh
xcode-select --install
brew install just uv node go gh
just setup
just
```

`.python-version` pins the build interpreter. `requirements.txt` pins runtime dependencies; `requirements-build.txt` includes them and pins packaging dependencies. The justfile pins Ruff and actionlint versions. Setup and the first tool runs download dependencies. Later checks run on your Mac without GitHub CI credits. Use uv-managed Python because some Homebrew runtimes require a newer macOS version than the app targets.

## Check, build, and package

```sh
just lint
just test
just test-minimum
just ci
```

`just ci` checks Python errors, workflow syntax, release metadata, and Python 3.10 compatibility. It then builds and verifies the app with the pinned build interpreter. The builder runs the full test suite, native checks, Mach-O compatibility checks, ad-hoc signing, and isolated ZIP verification. Tests use synthetic chats and temporary state, without installing login services or dispatching work.

Use `just package` or its alias `just build` to compile, sign, and package the verified app. To replace an existing local package, pass `--replace`. CI replaces local packages so checks can run repeatedly.

```sh
just package
just package --replace
just package --output '/tmp/Carrie On release'
just verify
just verify '/tmp/Carrie On release/Carrie-On-0.5.6-macos-arm64.zip'
```

The builder writes a versioned DMG, ZIP, a `.sha256` checksum for each, and a JSON manifest to `dist/`. The DMG provides the drag-to-Applications installation window. Built-in updates use the ZIP. Both packages contain the same signed app. The builder mounts the DMG read-only, checks the Applications shortcut, and verifies a copied app against the ZIP and its code signature.

The manifest records the version, source commit, build identity, architecture, Python version, compatibility results, ZIP size, and ZIP checksum. The source commit is `null` for dirty or exported source. The `disk_image` object records the DMG filename, size, and checksum. Build identities and signing timestamps prevent byte-for-byte reproducible builds. Git ignores generated files.

Keep the `vMAJOR.MINOR.PATCH` tag and generated asset names unchanged, and publish the ZIP and JSON manifest together. The updater offers only newer stable GitHub releases. It ignores drafts and prereleases. No additional update feed or credentials are required. The release manifest's hash and byte count must describe the final signed ZIP.

For an extracted app, signing and signature checks are also available directly:

```sh
just sign '/tmp/Carrie On.app'
just verify-signature '/tmp/Carrie On.app'
```

Signing is ad-hoc. It does not provide Developer ID signing or notarization. After `just sign` modifies an app, rebuild its archives and checksums. Use `just icons` to regenerate packaged icon sizes from the master artwork. Run `just doctor` to check local Codex connectivity without starting a turn.

## Local development app

Dev compares its built commit with the saved checkout's `HEAD` once on startup. If the checkout has newer commits, Settings shows **Update to &lt;short hash&gt;**. Otherwise it shows **Check for Updates**, which checks again when clicked. The check uses local Git history without fetching from GitHub. A rebuild includes uncommitted edits.

Run `just dev` to build Carrie On Dev into `dist/dev/`. Install it from the DMG
alongside Carrie On. To update it later, open Settings and choose
**Update to &lt;short hash&gt;**. This rebuilds the native interface, bundled Python,
and backend while displaying the build output.

The action runs `python -m tools.build_bundle --profile dev --prepare-app`. Tests and bundle
checks must pass before it replaces the app and restarts Dev's services. It
preserves the queue, settings, and login preference. A build failure leaves the
current app running and keeps the error output open. This command does not
create a DMG, pull commits, or switch branches.

Older dev builds need a fresh `just dev` build installed once to gain this
action and its saved checkout and interpreter paths. Reinstall from a new location
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
the same live Codex account. Use different chats when testing, or quit the other
app before enrolling the same chat. Separate application data does not isolate
the external Codex chats or account quota.

The release commands still build the release profile and reject development
artifacts. Publish versioned GitHub prereleases for beta testing, then mark the
validated release stable. There is no rolling beta feed. The existing in-app
updater offers stable releases only, so beta installation is manual. Dev never
installs a production update over itself.

Source services preserve the virtual-environment interpreter used for registration and native rebuilds; they do not depend on an activated shell. Registered source backends store data in the selected profile’s Application Support directory. Direct source servers default to checkout-local data. See [source installation and migration](INSTALL.md#update-a-source-installation) before switching between them.

## Publish a versioned release

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

If GitHub created the draft but an asset upload was interrupted, use `just upload` to replace its assets. Both recovery commands require packages matching the current clean commit and a local tag pointing to that commit. `just upload` rejects published releases. Use `just release-notes` to regenerate `dist/release-notes.md` for review. Retrying does not publish the draft.

Download and test the draft on another Mac using the [installation and release verification guide](INSTALL.md#release-verification). Record the actual macOS and Codex versions tested in its notes, including remaining compatibility limits. When satisfied:

```sh
just publish
```

Publishing requires the matching source, tag, packages, and an existing draft. Never move a published tag or replace published assets. Correct defects with a new patch version. Recipients can restore the previous app from its ZIP. Back up personal data before downgrading across a data-format migration.

## Command behavior

Run `just --list` to discover recipes and `just --dry-run release` to inspect the commands without executing them. Recipe dependencies handle ordering and run once per invocation. Paths are quoted and extra `package` arguments are forwarded unchanged. `just` stops at the first failed command. A failed check prevents later tag pushes and release creation.

The underlying `python -m tools.release [--tag TAG]` prints Markdown notes. Its validation flags check the following conditions:

- `--check-source` requires a clean Git commit.
- `--check-assets DIRECTORY` verifies that package metadata and checksums match the source and version.
- `--check-tag` requires a matching local tag.
- `--check-draft` requires a GitHub draft.

These checks do not change source, tags, or releases. File and validation failures return 1 with stderr diagnostics. Argument errors return 2. Without check flags, release metadata works outside a Git checkout. Git and GitHub CLI use their existing authentication.

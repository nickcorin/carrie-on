# Releasing Carrie On

`VERSION` owns the numeric `MAJOR.MINOR.PATCH` version. `CHANGELOG.md` must have exactly one nonempty `## MAJOR.MINOR.PATCH` section for that version. Git tags use `vMAJOR.MINOR.PATCH`. Use a new version for every published build; never move a published tag or replace its assets.

## Build locally

Use an Apple Silicon Mac with Apple's Swift command-line tools and [uv](https://docs.astral.sh/uv/). `.python-version` pins the build interpreter; `requirements-build.txt` pins the packaging dependencies. The uv-managed runtime has an older macOS deployment target than some Homebrew distributions. The binary audit rejects incompatible runtimes.

From the repository root:

```sh
uv python install
uv venv --python "$(cat .python-version)" .venv
uv pip install --python .venv/bin/python -r requirements-build.txt
.venv/bin/python release.py
.venv/bin/python build_bundle.py
```

The builder runs the Python tests, compiles and runs native self-tests, freezes the backend and dashboard, includes documentation and licenses, audits Mach-O deployment targets and library paths, and signs the completed app. It verifies the ZIP in an isolated home before copying results into `dist/`. It does not install the app, load login services, or dispatch chats.

For version 0.5.1, the output is:

```text
Carrie-On-0.5.1-macos-arm64.zip
Carrie-On-0.5.1-macos-arm64.zip.sha256
Carrie-On-0.5.1-macos-arm64.json
```

The JSON records build identity, architecture, Python version, compatibility audit, archive size, and checksum. Build IDs and signing timestamps make builds non-byte-reproducible. Check the received ZIP from its download directory:

```sh
shasum -a 256 -c Carrie-On-0.5.1-macos-arm64.zip.sha256
```

To rerun acceptance checks, choose another output directory, or intentionally replace local development artifacts:

```sh
.venv/bin/python verify_bundle.py dist/Carrie-On-0.5.1-macos-arm64.zip
.venv/bin/python build_bundle.py --output /tmp/carrie-on-release
.venv/bin/python build_bundle.py --replace
```

`--replace` affects only local output. Never use a rebuild to replace assets already published to recipients. `MINIMUM_MACOS` in `app_runtime.py` sets the shared deployment target. Linkage audits do not establish runtime compatibility; see the [manual release checks](INSTALL.md#release-verification).

## GitHub setup

Create `nickcorin/carrie-on` on GitHub with `master` as its default branch and GitHub Actions enabled. Push the reviewed source to `master`. The workflows require no custom credentials: only the draft-release job receives `contents: write` through GitHub's temporary token. CI and builds have read-only permissions, and checkout does not persist credentials.

CI runs on pull requests and pushes to `master`; it can also be dispatched manually. It tests Python 3.10 compatibility and builds the app on `macos-15` with the pinned Python runtime. Protect `master` using the checks shown by the first successful CI run. Enable private vulnerability reporting if desired.

Actions are pinned to immutable commits. Dependabot proposes action and Python dependency updates monthly. Review those updates through the same bundle acceptance checks.

## Prepare a release

Update `VERSION` and its changelog section, commit the change with a signed Conventional Commit, and get a passing CI build on `master`. Validate the intended tag before creating it:

```sh
.venv/bin/python release.py --tag v0.5.1
```

Create a signed tag on the reviewed commit and push it when ready to start the release build:

```sh
git tag -s v0.5.1 -m "Carrie On 0.5.1"
git push origin v0.5.1
```

The Release workflow validates the tag, runs the shared build and verification workflow, checks the transferred checksum, and creates a **draft** GitHub release with notes from the changelog and all three assets. A failed build cannot reach the draft job. Rerunning a failed workflow can update an existing draft; it refuses to update a published release. Wait for the workflow to finish before editing or publishing the draft.

Download and test the draft on another Mac using the [installation and release verification guide](INSTALL.md). Record the actual macOS and Codex versions tested in the release notes, along with remaining compatibility limits. The app currently uses ad-hoc signing and is not notarized. Publish the draft in GitHub only after those checks are satisfactory.

If a published release is defective, document the problem and issue a new patch version. Recipients can restore the previous app from its ZIP; back up user data before downgrading across data-format changes.

## Release metadata command

`python3 release.py [--tag TAG]` reads `VERSION` and `CHANGELOG.md` beside the script and prints Markdown release notes to stdout. The optional string `--tag` must exactly match the derived version tag. It makes no Git, network, or installation changes and is safe to repeat. Redirect stdout to save notes:

```sh
python3 release.py --help
python3 release.py
python3 release.py --tag v0.5.1
python3 release.py --tag v0.5.1 > /tmp/carrie-on-release-notes.md
python3 release.py | less
```

Exit status is 0 for valid metadata, 1 for file or validation errors (reported on stderr), and 2 for invalid arguments. There is no environment or per-user configuration.

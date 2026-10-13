set positional-arguments

python := '.venv/bin/python'
version := trim(read('VERSION'))
tag := 'v' + version
artifact := 'Carrie-On-' + version + '-macos-arm64'
archive := 'dist/' + artifact + '.zip'
image := 'dist/' + artifact + '.dmg'
manifest := 'dist/' + artifact + '.json'
notes := 'dist/release-notes.md'

# List the available development and release commands.
default:
    @just --list

# Install the pinned Python runtime and packaging dependencies.
setup:
    uv python install
    uv venv --allow-existing --python {{ trim(read('.python-version')) }} .venv
    uv pip install --python {{ python }} -r requirements-build.txt

# Run the full local CI gate; never dispatch GitHub Actions.
ci: lint secrets test-minimum (package '--replace')

alias check := ci
alias build := package

# Check Python, JavaScript, workflows, and release metadata.
lint: lint-just lint-python lint-web lint-workflows release-check

lint-just:
    just --fmt --check

# Scan committed history for secrets without uploading source.
secrets:
    go run github.com/zricethezav/gitleaks/v8@v8.30.1 git --redact --no-banner .

lint-python:
    uvx --from ruff==0.17.0 ruff check --select F,E9 .

lint-web:
    node --check web/app.js

lint-workflows:
    go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.12

# Run all Python and native pagination tests using the build interpreter.
test: setup
    {{ python }} -m unittest discover -v

# Exercise the documented minimum Python version.
test-minimum:
    uv run --no-project --python 3.10 python -m unittest discover -v

# Build, test, ad-hoc sign, package, and verify the app. Accepts builder flags.
package *args: setup
    {{ python }} build_bundle.py "$@"

# Sign an existing app; repackage afterwards because signing changes its bytes.
sign app: setup
    {{ python }} build_bundle.py --sign-app {{ quote(app) }}

# Check an existing app's nested code signatures.
verify-signature app:
    /usr/bin/codesign --verify --deep --strict {{ quote(app) }}

# Run the complete isolated acceptance check on an existing ZIP.
verify path=archive: setup
    {{ python }} verify_bundle.py {{ quote(path) }}

# Check the release version and changelog without changing files.
release-check: setup
    {{ python }} release.py --tag {{ quote(tag) }}

# Write the current release notes for GitHub.
release-notes: release-check
    mkdir -p dist
    {{ python }} release.py --tag {{ quote(tag) }} > {{ quote(notes) }}

# Require committed source before creating a release.
release-ready: setup
    {{ python }} release.py --check-source

# Run local CI, create a signed tag, push it, and upload a draft release.
release: release-ready ci release-notes
    {{ python }} release.py --check-assets dist
    git tag -s {{ quote(tag) }} -m {{ quote('Carrie On ' + version) }}
    git push origin {{ quote(tag) }}
    gh release create {{ quote(tag) }} {{ quote(archive) }} {{ quote(archive + '.sha256') }} {{ quote(image) }} {{ quote(image + '.sha256') }} {{ quote(manifest) }} --verify-tag --draft --title {{ quote('Carrie On ' + version) }} --notes-file {{ quote(notes) }}

# Retry a failed tag push or draft creation without rebuilding or retagging.
release-retry: release-ready release-notes
    {{ python }} release.py --check-assets dist --check-tag
    git push origin {{ quote(tag) }}
    gh release create {{ quote(tag) }} {{ quote(archive) }} {{ quote(archive + '.sha256') }} {{ quote(image) }} {{ quote(image + '.sha256') }} {{ quote(manifest) }} --verify-tag --draft --title {{ quote('Carrie On ' + version) }} --notes-file {{ quote(notes) }}

# Replace assets only on an existing draft after an interrupted upload.
upload: release-ready
    {{ python }} release.py --check-assets dist --check-draft --check-tag
    gh release upload {{ quote(tag) }} {{ quote(archive) }} {{ quote(archive + '.sha256') }} {{ quote(image) }} {{ quote(image + '.sha256') }} {{ quote(manifest) }} --clobber

# Publish an existing draft after manual acceptance testing.
publish: release-ready
    {{ python }} release.py --check-assets dist --check-draft --check-tag
    gh release edit {{ quote(tag) }} --draft=false

# Regenerate app and dashboard icons from the master artwork.
icons:
    python3 build_icons.py

# Inspect local connectivity without dispatching chats or installing services.
doctor: setup
    {{ python }} tokenmaxxer.py doctor

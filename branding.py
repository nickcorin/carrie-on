"""Reject retired branding in source inputs and packaged application payloads."""

import marshal
import plistlib
import subprocess
from pathlib import Path

from legacy_migration import PREVIOUS_IDENTIFIER


# Keep the rejected spelling out of shipped source and bytecode constants.
RETIRED_NAME = bytes.fromhex("746f6b656e6d6178786572")


def check_branding(name, payload=b""):
    """Check a path and its payload, including common Unicode encodings."""
    spelling = RETIRED_NAME.decode("ascii")
    patterns = (RETIRED_NAME, spelling.encode("utf-16-le"), spelling.encode("utf-16-be"))
    if RETIRED_NAME in str(name).lower().encode() or any(pattern in payload.lower() for pattern in patterns):
        raise ValueError(f"Retired app branding in {name}")


def check_source(root):
    """Check current tracked and nonignored inputs without reading Git history."""
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
    ).decode().split("\0")
    for name in sorted(set(names) - {""}):
        path = root / name
        if path.is_file():
            # Historical identifiers belong only to the migration and its fixtures.
            payload = b"" if name in {"legacy_migration.py", "test_legacy_migration.py"} else path.read_bytes()
            check_branding(name, payload)


def check_bundle(app):
    """Check files, symlink targets, and compressed Python modules in an app."""
    from PyInstaller.archive.readers import CArchiveReader

    for path in app.rglob("*"):
        name = path.relative_to(app)
        if path.is_symlink():
            check_branding(name, str(path.readlink()).encode())
        elif path.is_file():
            payload = path.read_bytes()
            if name == Path("Contents/Info.plist"):
                info = plistlib.loads(payload)
                if info.get("CarrieOnPreviousBundleIdentifier") == PREVIOUS_IDENTIFIER:
                    del info["CarrieOnPreviousBundleIdentifier"]
                payload = plistlib.dumps(info)
            check_branding(name, payload)

    archive = CArchiveReader(str(app / "Contents/MacOS/carrie_on"))
    for name, entry in archive.toc.items():
        check_branding(name)
        if entry[-1] == "z":
            modules = archive.open_embedded_archive(name)
            for module in modules.toc:
                payload = b"" if module == "legacy_migration" else marshal.dumps(modules.extract(module))
                check_branding(module, payload)
        else:
            check_branding(name, archive.extract(name) or b"")


if __name__ == "__main__":
    check_source(Path(__file__).resolve().parent)
    print("PASS: current source inputs use Carrie On branding")

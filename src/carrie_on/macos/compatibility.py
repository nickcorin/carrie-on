"""Check the deployment targets and dynamic-library paths in a macOS payload."""

import re
import subprocess

MACH_HEADERS = {
    bytes.fromhex(value)
    for value in (
        "feedface",
        "cefaedfe",
        "feedfacf",
        "cffaedfe",
        "cafebabe",
        "bebafeca",
        "cafebabf",
        "bfbafeca",
    )
}


def version_tuple(value):
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * (3 - len(parts))


def deployment_versions(load_commands):
    versions = []
    command = ""
    for line in load_commands.splitlines():
        words = line.split()
        if words[:1] == ["cmd"]:
            command = words[1]
        version_field = {"LC_BUILD_VERSION": "minos", "LC_VERSION_MIN_MACOSX": "version"}.get(
            command
        )
        if words[:1] == [version_field]:
            versions.append(words[1])
    return versions


def audit(payload, minimum_macos):
    """Inspect a payload with otool; reject newer targets or non-system absolute libraries.

    Return binary count and highest deployment target. This checks linkage metadata,
    not runtime behavior on the target OS. Raise ValueError for incompatibility.
    """
    inspected = set()
    highest = "0.0"
    for path in sorted(payload.rglob("*")):
        if not path.is_file() or path.resolve() in inspected:
            continue
        with path.open("rb") as stream:
            if stream.read(4) not in MACH_HEADERS:
                continue
        inspected.add(path.resolve())
        commands = subprocess.check_output(["/usr/bin/otool", "-l", str(path)], text=True)
        versions = deployment_versions(commands)
        if not versions:
            raise ValueError(f"No macOS deployment target found in {path.name}")
        for version in versions:
            if version_tuple(version) > version_tuple(minimum_macos):
                raise ValueError(
                    f"{path.relative_to(payload)} requires macOS {version}, above the "
                    f"{minimum_macos} target. Build with a compatible Python distribution."
                )
            if version_tuple(version) > version_tuple(highest):
                highest = version
        libraries = subprocess.check_output(["/usr/bin/otool", "-L", str(path)], text=True)
        for dependency in re.findall(r"^\s+(.+?) \(compatibility version", libraries, re.MULTILINE):
            if not dependency.startswith(("@", "/usr/lib/", "/System/Library/")):
                raise ValueError(
                    f"{path.relative_to(payload)} depends on an external library: {dependency}"
                )
    if not inspected:
        raise ValueError("No Mach-O binaries found in the bundle")
    return {"binaries_checked": len(inspected), "highest_deployment_target": highest}

#!/usr/bin/env bash
# Removes only the owned user installation; source checkouts and bags are retained.
set -euo pipefail
"${PYTHON:-python3}" - "$@" <<'NEOBAG_PYTHON'
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys

OWNER = "neobag-installer-v1"
MARKER = ".neobag-install.json"
RELEASE_MARKER = ".neobag-release.json"


def fail(message):
    raise RuntimeError(message)


def checked_path(value, home, label):
    path = Path(value).expanduser()
    if (not path.is_absolute() or ".." in path.parts or path == home or home not in path.parents
            or any(ord(char) < 32 for char in str(path))):
        fail(f"{label} must be an absolute directory strictly below HOME: {path}")
    for component in (path, *path.parents):
        if component.is_symlink():
            fail(f"Refusing symlink in {label}: {component}")
        if component.exists() and not component.is_dir():
            fail(f"{label} contains a non-directory: {component}")
    return path


def read_marker(path, expected):
    if path.is_symlink() or not path.is_file():
        fail(f"Missing ownership marker; preserving {path.parent}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail(f"Invalid ownership marker; preserving {path.parent}")
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
        fail(f"Ownership marker does not match; preserving {path.parent}")


def release_owned(path, prefix):
    if path.is_symlink() or not path.is_dir() or not re.fullmatch(r"r-[0-9a-f]{32}", path.name):
        fail(f"Unrecognized release; preserving {path}")
    read_marker(path / RELEASE_MARKER, {"owner": OWNER, "prefix": str(prefix), "release": path.name})
    if any(item.name not in {RELEASE_MARKER, "venv", "source"} for item in path.iterdir()):
        fail(f"Extra files in release; preserving {path}")


def protect_bag_data(prefix):
    def unreadable(error):
        raise error

    # A user can choose any recording output path, including a nested managed
    # source/venv directory. Complete this check before unlinking the launcher.
    for directory, subdirectories, filenames in os.walk(prefix, followlinks=False, onerror=unreadable):
        subdirectories[:] = [name for name in subdirectories if not (Path(directory) / name).is_symlink()]
        for name in filenames:
            lower = name.lower()
            if lower == "metadata.yaml" or lower.endswith((".db3", ".db3-wal", ".mcap", ".bag")):
                fail(
                    f"ROS bag data found: {Path(directory) / name}. "
                    f"Move bag data outside {prefix}, then retry uninstall. "
                    "The complete installation was preserved."
                )


def legacy_launcher(text):
    lines = text.splitlines()
    if len(lines) != 3 or lines[:2] != ["#!/usr/bin/env bash", "# neobag managed launcher"]:
        return False
    try:
        tokens = shlex.split(lines[2])
    except ValueError:
        return False
    return (len(tokens) == 3 and tokens[0] == "exec" and tokens[2] == "$@"
            and Path(tokens[1]).is_absolute() and tokens[1].endswith("/.venv/bin/neobag"))


def check_launcher(launcher, prefix):
    if launcher.is_symlink():
        fail(f"Refusing symlinked launcher; preserving {launcher}")
    if not launcher.exists():
        return False
    if not launcher.is_file():
        fail(f"Launcher is not a regular file; preserving {launcher}")
    text = launcher.read_text(encoding="utf-8")
    if legacy_launcher(text):
        return True
    lines = text.splitlines()
    if len(lines) == 4 and lines[:3] == ["#!/usr/bin/env bash", "# neobag managed launcher v1", f"# prefix: {prefix}"]:
        try:
            tokens = shlex.split(lines[3])
        except ValueError:
            tokens = []
        if len(tokens) == 3 and tokens[0] == "exec" and tokens[2] == "$@":
            command = Path(tokens[1])
            if len(command.parents) >= 3:
                release = command.parents[2]
                if release.parent == prefix / "releases" and command == release / "venv/bin/neobag":
                    release_owned(release, prefix)
                    return True
    fail(f"Launcher is not owned by neobag; preserving {launcher}")


def main():
    argparse.ArgumentParser(description="Uninstall only neobag-owned user files; keeps source, bags and evidence.").parse_args()
    home = Path(os.environ.get("HOME", "")).expanduser()
    if not home.is_absolute() or not home.is_dir() or home.is_symlink() or ".." in home.parts:
        fail("HOME must be an existing absolute directory, without symlinks or traversal")
    prefix = checked_path(os.environ.get("NEOBAG_HOME", str(home / ".local/share/neobag")), home, "NEOBAG_HOME")
    bin_dir = checked_path(os.environ.get("NEOBAG_BIN_DIR", str(home / ".local/bin")), home, "NEOBAG_BIN_DIR")
    if prefix == bin_dir or prefix in bin_dir.parents or bin_dir in prefix.parents:
        fail("NEOBAG_HOME and NEOBAG_BIN_DIR must be separate directories")
    launcher = bin_dir / "neobag"
    if prefix.exists():
        read_marker(prefix / MARKER, {"owner": OWNER, "prefix": str(prefix), "bin_dir": str(bin_dir)})
        protect_bag_data(prefix)
    owned_launcher = check_launcher(launcher, prefix)
    if prefix.exists():
        if any(item.name not in {MARKER, "releases", "uninstall.sh"} for item in prefix.iterdir()):
            fail(f"Unrecognized files in managed directory; preserving {prefix}")
        releases = prefix / "releases"
        if releases.is_symlink() or (releases.exists() and not releases.is_dir()):
            fail(f"Refusing invalid releases directory: {releases}")
        if (prefix / "uninstall.sh").is_symlink():
            fail("Refusing symlinked managed uninstaller")
        if releases.exists():
            for release in releases.iterdir():
                release_owned(release, prefix)
        # Validate before removing anything. rmtree unlinks nested venv symlinks
        # without following them into the system Python installation.
        if owned_launcher:
            launcher.unlink()
        shutil.rmtree(prefix)
        print("neobag uninstalled. Source checkouts, legacy .venv, bags and evidence were preserved.")
    elif owned_launcher:
        launcher.unlink()
        print("Legacy neobag launcher removed. Its source checkout and .venv were preserved.")
    else:
        print("No managed neobag installation found; nothing was removed.")


try:
    main()
except (RuntimeError, OSError, ValueError) as error:
    print(f"neobag uninstall: {error}", file=sys.stderr)
    raise SystemExit(1)
NEOBAG_PYTHON

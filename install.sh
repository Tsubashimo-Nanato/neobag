#!/usr/bin/env bash
# Works from a checkout or through: curl -fsSL <raw URL>/install.sh | bash
set -euo pipefail
export NEOBAG_INSTALLER_PATH="${BASH_SOURCE[0]:-}"
"${PYTHON:-python3}" - "$@" <<'NEOBAG_PYTHON'
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile

OWNER = "neobag-installer-v1"
MARKER = ".neobag-install.json"
RELEASE_MARKER = ".neobag-release.json"
REPOSITORY = "Tsubashimo-Nanato/neobag"


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
    return data


def release_owned(path, prefix):
    if path.is_symlink() or not path.is_dir() or not re.fullmatch(r"r-[0-9a-f]{32}", path.name):
        fail(f"Unrecognized release; preserving {path}")
    read_marker(path / RELEASE_MARKER, {"owner": OWNER, "prefix": str(prefix), "release": path.name})
    if any(item.name not in {RELEASE_MARKER, "venv", "source"} for item in path.iterdir()):
        fail(f"Extra files in release; preserving {path}")


def check_prefix(prefix, bin_dir):
    if not prefix.exists():
        return
    read_marker(prefix / MARKER, {"owner": OWNER, "prefix": str(prefix), "bin_dir": str(bin_dir)})
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
        return
    if not launcher.is_file():
        fail(f"Launcher is not a regular file; preserving {launcher}")
    text = launcher.read_text(encoding="utf-8")
    if legacy_launcher(text):
        return
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
                    return
    fail(f"Launcher is not owned by neobag; preserving {launcher}")


def atomic_write(path, content, mode=0o644):
    descriptor, temporary = tempfile.mkstemp(prefix=".neobag-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def download_source(destination, reference):
    if not reference or len(reference) > 256 or any(ord(char) < 32 for char in reference):
        fail("Invalid Git reference")
    url = f"https://codeload.github.com/{REPOSITORY}/zip/{urllib.parse.quote(reference, safe='')}"
    request = urllib.request.Request(url, headers={"User-Agent": "neobag-installer/1"})
    print(f"Downloading {REPOSITORY} at {reference} ...", flush=True)
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        fail("Source archive exceeds the 32 MiB limit")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if sum(entry.file_size for entry in entries) > 128 * 1024 * 1024:
            fail("Expanded source archive exceeds the 128 MiB limit")
        roots = set()
        for entry in entries:
            member = PurePosixPath(entry.filename)
            original = entry.orig_filename
            if (not member.parts or member.is_absolute() or ".." in member.parts
                    or "\\" in original or ":" in original or any(ord(char) < 32 for char in original)):
                fail("Unsafe path in source archive")
            if stat.S_ISLNK(entry.external_attr >> 16):
                fail("Symlinks are not accepted in source archives")
            roots.add(member.parts[0])
        if len(roots) != 1:
            fail("Unexpected source archive layout")
        for entry in entries:
            member = PurePosixPath(entry.filename)
            if len(member.parts) < 2:
                continue
            relative = PurePosixPath(*member.parts[1:])
            if relative.parts[0] not in {"src", "pyproject.toml", "README.md", "LICENSE", "uninstall.sh"}:
                continue
            output = destination.joinpath(*relative.parts)
            if entry.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, output.open("wb") as target:
                    shutil.copyfileobj(source, target)
    return destination


def copy_source(source, destination):
    if not (source / "pyproject.toml").is_file() or not (source / "src/neobag/__main__.py").is_file():
        fail(f"Not a neobag source directory: {source}")
    for item in (source / "src", *(source / "src").rglob("*")):
        if item.is_symlink():
            fail(f"Symlinked package source is not accepted: {item}")
    destination.mkdir()
    shutil.copytree(source / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        item = source / name
        if item.is_symlink():
            fail(f"Symlinked package file is not accepted: {item}")
        if item.is_file():
            shutil.copy2(item, destination / name)


def main():
    parser = argparse.ArgumentParser(description="Install neobag in user-owned storage; never uses sudo.")
    parser.add_argument("--ref", default=os.environ.get("NEOBAG_REF"), help="Git tag, branch or commit SHA (default main for a piped install)")
    parser.add_argument("--source", type=Path, help="Install an existing local checkout without downloading GitHub source")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        fail("Python 3.10 or newer is required")
    if args.source is not None and args.ref is not None:
        fail("Use either --source or --ref, not both")
    home = Path(os.environ.get("HOME", "")).expanduser()
    if not home.is_absolute() or not home.is_dir() or home.is_symlink() or ".." in home.parts:
        fail("HOME must be an existing absolute directory, without symlinks or traversal")
    prefix = checked_path(os.environ.get("NEOBAG_HOME", str(home / ".local/share/neobag")), home, "NEOBAG_HOME")
    bin_dir = checked_path(os.environ.get("NEOBAG_BIN_DIR", str(home / ".local/bin")), home, "NEOBAG_BIN_DIR")
    if prefix == bin_dir or prefix in bin_dir.parents or bin_dir in prefix.parents:
        fail("NEOBAG_HOME and NEOBAG_BIN_DIR must be separate directories")
    launcher = bin_dir / "neobag"
    check_prefix(prefix, bin_dir)
    check_launcher(launcher, prefix)
    source = args.source
    installer = os.environ.get("NEOBAG_INSTALLER_PATH", "")
    if source is None and args.ref is None and installer and Path(installer).is_file():
        candidate = Path(installer).absolute().parent
        if (candidate / "pyproject.toml").is_file():
            source = candidate
    with tempfile.TemporaryDirectory(prefix="neobag-download-") as temporary:
        if source is None:
            source = download_source(Path(temporary), args.ref or "main")
            source_ref = args.ref or "main"
        else:
            source = source.expanduser().absolute()
            source_ref = "local source"
        uninstaller = source / "uninstall.sh"
        if uninstaller.is_symlink() or not uninstaller.is_file():
            fail("Source is missing a regular uninstall.sh; preserving the existing installation")
        uninstall_text = uninstaller.read_text(encoding="utf-8")
        prefix.mkdir(parents=True, exist_ok=True)
        (prefix / "releases").mkdir(exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        if not (prefix / MARKER).exists():
            atomic_write(prefix / MARKER, json.dumps({"owner": OWNER, "prefix": str(prefix), "bin_dir": str(bin_dir)}) + "\n")
        release = prefix / "releases" / ("r-" + uuid.uuid4().hex)
        release.mkdir()
        atomic_write(release / RELEASE_MARKER, json.dumps({"owner": OWNER, "prefix": str(prefix), "release": release.name, "source_ref": source_ref}) + "\n")
        try:
            copy_source(source, release / "source")
            environment = release / "venv"
            child_env = {key: value for key, value in os.environ.items()
                         if not key.startswith("PIP_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
            child_env["PIP_CONFIG_FILE"] = os.devnull
            child_env["PYTHONNOUSERSITE"] = "1"
            print("Creating the private Python environment ...", flush=True)
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, env=child_env)
            subprocess.run([str(environment / "bin/python"), "-m", "pip", "install", "--disable-pip-version-check", "--no-user", "--prefix", str(environment), str(release / "source")], check=True, env=child_env)
            command = environment / "bin/neobag"
            if command.is_symlink() or not command.is_file():
                fail("Installation did not create a regular neobag entry point")
            subprocess.run([str(command), "--version"], check=True, env=child_env)
            check_launcher(launcher, prefix)
            atomic_write(prefix / "uninstall.sh", uninstall_text, 0o755)
            atomic_write(launcher, "#!/usr/bin/env bash\n# neobag managed launcher v1\n" + f"# prefix: {prefix}\nexec {shlex.quote(str(command))} \"$@\"\n", 0o755)
        except BaseException:
            # Only this freshly created managed release is removed on failure.
            shutil.rmtree(release)
            raise
    print(f"Installed: {launcher}")
    print("Source your ROS 2 environment, then run: neobag")
    print(f"If needed: export PATH={shlex.quote(str(bin_dir))}:\"$PATH\"")
    print(f"Uninstall: NEOBAG_HOME={shlex.quote(str(prefix))} NEOBAG_BIN_DIR={shlex.quote(str(bin_dir))} bash {shlex.quote(str(prefix / 'uninstall.sh'))}")
    print("Your source checkout, existing .venv, bags and evidence were preserved.")


try:
    main()
except (RuntimeError, OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
    print(f"neobag install: {error}", file=sys.stderr)
    raise SystemExit(1)
NEOBAG_PYTHON

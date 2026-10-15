"""Installer safety tests without network or package-manager writes.

The embedded Python payload is the exact payload executed by Bash. Subprocesses
are mocked here; the real Linux install/uninstall round trip is a separate test.
"""

import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
from types import SimpleNamespace
import urllib.request
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def payload(script):
    text = (ROOT / script).read_text(encoding="utf-8")
    return text.split("<<'NEOBAG_PYTHON'\n", 1)[1].rsplit("\nNEOBAG_PYTHON", 1)[0]


@pytest.fixture
def installation(tmp_path, monkeypatch):
    home = tmp_path / "home with spaces"
    home.mkdir()
    source = home / "source checkout"
    (source / "src/neobag").mkdir(parents=True)
    (source / "src/neobag/__main__.py").write_text("print('neobag test')\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("[project]\nname = 'neobag'\nversion = '0.0.0'\n", encoding="utf-8")
    (source / "uninstall.sh").write_text((ROOT / "uninstall.sh").read_text(encoding="utf-8"), encoding="utf-8")
    (source / ".venv").mkdir()
    (source / ".venv/keep.txt").write_text("legacy environment", encoding="utf-8")
    (home / "bags").mkdir()
    (home / "bags/keep.db3").write_bytes(b"original rosbag")
    (source / "evidence").mkdir()
    (source / "evidence/keep.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    for name in ("NEOBAG_HOME", "NEOBAG_BIN_DIR", "NEOBAG_REF", "NEOBAG_INSTALLER_PATH"):
        monkeypatch.delenv(name, raising=False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["-m", "venv"]:
            environment = Path(command[3])
            (environment / "bin").mkdir(parents=True)
            (environment / "bin/python").write_text("fake python", encoding="utf-8")
        elif "pip" in command:
            environment = Path(command[0]).parents[1]
            (environment / "bin/neobag").write_text("#!/bin/sh\necho neobag-test\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    def run(script="install.sh", args=None, expected=0):
        if args is None:
            args = ["--source", str(source)] if script == "install.sh" else []
        monkeypatch.setattr(sys, "argv", [script, *args])
        status = 0
        try:
            exec(compile(payload(script), script, "exec"), {"__name__": "__main__"})
        except SystemExit as error:
            status = error.code
        assert status == expected

    return SimpleNamespace(
        home=home, source=source, prefix=home / ".local/share/neobag",
        launcher=home / ".local/bin/neobag", calls=calls, run=run,
    )


def test_install_upgrade_uninstall_preserves_source_bags_and_legacy_venv(installation):
    item = installation
    item.run()
    first_launcher = item.launcher.read_text(encoding="utf-8")
    assert "# neobag managed launcher v1" in first_launcher
    assert len(list((item.prefix / "releases").iterdir())) == 1
    assert (item.prefix / "uninstall.sh").is_file()
    command = Path(shlex.split(first_launcher.splitlines()[-1])[1])
    assert command.is_file()
    assert command.is_relative_to(item.prefix)
    item.run()
    assert item.launcher.read_text(encoding="utf-8") != first_launcher
    assert len(list((item.prefix / "releases").iterdir())) == 2
    item.run("uninstall.sh")
    assert not item.prefix.exists()
    assert not item.launcher.exists()
    assert (item.source / "src/neobag/__main__.py").is_file()
    assert (item.source / ".venv/keep.txt").read_text(encoding="utf-8") == "legacy environment"
    assert (item.home / "bags/keep.db3").read_bytes() == b"original rosbag"
    assert (item.source / "evidence/keep.json").read_text(encoding="utf-8") == "{}"
    item.run("uninstall.sh")  # repeat removal is harmless


def test_pip_failure_keeps_working_launcher(installation, monkeypatch):
    item = installation
    item.run()
    original = item.launcher.read_bytes()
    original_releases = list((item.prefix / "releases").iterdir())

    def broken_pip(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", broken_pip)
    item.run(expected=1)
    assert item.launcher.read_bytes() == original
    assert list((item.prefix / "releases").iterdir()) == original_releases


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
def test_unowned_launcher_is_not_overwritten_or_deleted(installation, script):
    item = installation
    item.launcher.parent.mkdir(parents=True)
    content = "#!/bin/sh\necho unrelated tool\n# neobag managed launcher\n"
    item.launcher.write_text(content, encoding="utf-8")
    item.run(script, expected=1)
    assert item.launcher.read_text(encoding="utf-8") == content
    assert not item.prefix.exists()


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
def test_unowned_prefix_is_never_removed(installation, script):
    item = installation
    item.prefix.mkdir(parents=True)
    sentinel = item.prefix / "keep.txt"
    sentinel.write_text("private", encoding="utf-8")
    item.run(script, expected=1)
    assert sentinel.read_text(encoding="utf-8") == "private"


def test_uninstall_refuses_unrecognized_files_before_removing_launcher(installation):
    item = installation
    item.run()
    sentinel = item.prefix / "important.db3"
    sentinel.write_bytes(b"bag")
    item.run("uninstall.sh", expected=1)
    assert sentinel.read_bytes() == b"bag"
    assert item.launcher.is_file()


def test_uninstall_refuses_extra_release_files(installation):
    item = installation
    item.run()
    release = next((item.prefix / "releases").iterdir())
    sentinel = release / "bag.db3"
    sentinel.write_bytes(b"bag")
    item.run("uninstall.sh", expected=1)
    assert sentinel.is_file()
    assert item.launcher.is_file()


@pytest.mark.parametrize("location", ["source/bags/recording", "venv/cache/recording"])
@pytest.mark.parametrize("filename", ["metadata.yaml", "recording.db3", "recording.db3-wal", "recording.mcap", "recording.bag"])
def test_uninstall_preserves_complete_installation_when_nested_bag_exists(installation, capsys, location, filename):
    item = installation
    item.run()
    release = next((item.prefix / "releases").iterdir())
    bag = release / location / filename
    bag.parent.mkdir(parents=True)
    bag.write_bytes(b"irreplaceable recording")
    before = {path.relative_to(item.prefix): path.read_bytes() for path in item.prefix.rglob("*") if path.is_file()}
    launcher_before = item.launcher.read_bytes()
    item.run("uninstall.sh", expected=1)
    after = {path.relative_to(item.prefix): path.read_bytes() for path in item.prefix.rglob("*") if path.is_file()}
    assert after == before
    assert item.launcher.read_bytes() == launcher_before
    assert bag.read_bytes() == b"irreplaceable recording"
    message = capsys.readouterr().err
    assert "Move bag data outside" in message
    assert "complete installation was preserved" in message


def test_uninstall_bag_scan_does_not_follow_symlink_directories(installation):
    item = installation
    item.run()
    release = next((item.prefix / "releases").iterdir())
    outside = item.home / "external-bags"
    outside.mkdir()
    bag = outside / "recording.mcap"
    bag.write_bytes(b"external recording")
    try:
        (release / "venv/external").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires platform permission")
    item.run("uninstall.sh")
    assert not item.prefix.exists()
    assert not item.launcher.exists()
    assert bag.read_bytes() == b"external recording"


@pytest.mark.parametrize("setting", ["NEOBAG_HOME", "NEOBAG_BIN_DIR"])
@pytest.mark.parametrize("outside", [True, False])
def test_prefix_overrides_cannot_escape_home(installation, monkeypatch, setting, outside):
    item = installation
    target = item.home.parent / "outside" if outside else item.home / ".." / "outside"
    monkeypatch.setenv(setting, str(target))
    item.run(expected=1)
    assert not target.exists()


def test_prefix_newline_is_rejected_before_launcher_creation(installation, monkeypatch):
    item = installation
    monkeypatch.setenv("NEOBAG_HOME", str(item.home / "new\ncommand"))
    item.run(expected=1)
    assert not item.launcher.exists()


def test_pip_environment_cannot_redirect_installation(installation, monkeypatch):
    item = installation
    monkeypatch.setenv("PIP_TARGET", "/unrelated")
    monkeypatch.setenv("PIP_CONFIG_FILE", "/unrelated/config")
    original = subprocess.run
    environments = []

    def capture(command, **kwargs):
        environments.append(kwargs["env"])
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture)
    item.run()
    assert all("PIP_TARGET" not in environment for environment in environments)
    assert all(environment["PIP_CONFIG_FILE"] == os.devnull for environment in environments)
    pip_command = next(command for command in item.calls if "pip" in command)
    assert "--no-user" in pip_command
    assert Path(pip_command[pip_command.index("--prefix") + 1]).is_relative_to(item.prefix)


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
def test_symlinked_launcher_is_preserved(installation, script):
    item = installation
    item.launcher.parent.mkdir(parents=True)
    target = item.home / "other-tool"
    target.write_text("keep", encoding="utf-8")
    try:
        item.launcher.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks requires platform permission")
    item.run(script, expected=1)
    assert item.launcher.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


def test_symlinked_prefix_is_preserved(installation):
    item = installation
    target = item.home / "keep-directory"
    target.mkdir()
    (target / "keep").write_text("keep", encoding="utf-8")
    item.prefix.parent.mkdir(parents=True)
    try:
        item.prefix.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires platform permission")
    item.run(expected=1)
    item.run("uninstall.sh", expected=1)
    assert (target / "keep").is_file()


@pytest.mark.skipif(os.name == "nt", reason="Legacy launcher paths are POSIX shell paths")
def test_exact_legacy_launcher_migrates_and_keeps_old_venv(installation):
    item = installation
    item.launcher.parent.mkdir(parents=True)
    target = item.source / ".venv/bin/neobag"
    item.launcher.write_text(f"#!/usr/bin/env bash\n# neobag managed launcher\nexec {shlex.quote(str(target))} \"$@\"\n", encoding="utf-8")
    item.run()
    assert "# neobag managed launcher v1" in item.launcher.read_text(encoding="utf-8")
    item.run("uninstall.sh")
    assert (item.source / ".venv/keep.txt").is_file()


def archive_bytes(source, extra=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name in ("pyproject.toml", "src/neobag/__main__.py", "uninstall.sh"):
            archive.writestr(f"neobag-sha/{name}", (source / name).read_bytes())
        if extra is not None:
            name, content = extra
            if isinstance(name, str):
                # ZipInfo normalizes backslashes on Windows. Set filename after
                # construction to model an actual unsafe archive from elsewhere.
                info = zipfile.ZipInfo("placeholder")
                info.filename = name
                archive.writestr(info, content)
            else:
                archive.writestr(name, content)
    return data.getvalue()


def test_piped_install_downloads_requested_ref_and_records_it(installation, monkeypatch):
    item = installation
    calls = []

    def download(request, **kwargs):
        calls.append((request.full_url, kwargs))
        return io.BytesIO(archive_bytes(item.source))

    monkeypatch.setattr(urllib.request, "urlopen", download)
    item.run(args=["--ref", "release/test"])
    assert calls == [("https://codeload.github.com/Tsubashimo-Nanato/neobag/zip/release%2Ftest", {"timeout": 30})]
    release = next((item.prefix / "releases").iterdir())
    marker = json.loads((release / ".neobag-release.json").read_text(encoding="utf-8"))
    assert marker["source_ref"] == "release/test"
    assert item.launcher.is_file()


@pytest.mark.parametrize("name", ["neobag-sha/../../escaped", "/absolute", "neobag-sha/src\\escape"])
def test_archive_traversal_is_rejected(installation, monkeypatch, name):
    item = installation
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(archive_bytes(item.source, (name, "bad"))))
    item.run(args=["--ref", "main"], expected=1)
    assert not item.prefix.exists()


def test_archive_symlink_is_rejected(installation, monkeypatch):
    item = installation
    link = zipfile.ZipInfo("neobag-sha/src/neobag/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(archive_bytes(item.source, (link, "/etc"))))
    item.run(args=["--ref", "main"], expected=1)
    assert not item.prefix.exists()


def test_local_checkout_is_auto_detected(installation, monkeypatch):
    item = installation
    script = item.source / "install.sh"
    script.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("NEOBAG_INSTALLER_PATH", str(script))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("Local install must not download source"))
    item.run(args=[])
    assert item.launcher.is_file()

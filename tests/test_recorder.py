"""Recorder safety checks and child-process integration, without ROS/hardware."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time
from unittest.mock import Mock

import pytest

from neobag.recorder import Recorder, RecorderError


FAKE_ROS2 = r'''
import json, os, signal, sqlite3, sys, time
from pathlib import Path
mode = sys.argv[1]
arguments = sys.argv[2:]
output = Path(arguments[arguments.index("--output") + 1])
output.mkdir()
(output / "arguments.json").write_text(json.dumps(arguments))
(output / "stdin_is_tty").write_text(str(sys.stdin.isatty()))
def saved_metadata():
    with sqlite3.connect(output / "bag_0.db3") as db:
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER, timestamp INTEGER, data BLOB)")
        db.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT, serialization_format TEXT)")
    count = 3 if mode == "foxy-bad-count" else 0
    (output / "metadata.yaml").write_text("rosbag2_bagfile_information:\n  version: 4\n  storage_identifier: sqlite3\n  relative_file_paths: [bag_0.db3]\n  message_count: " + str(count) + "\n")
if mode == "crash":
    print("storage plugin failed", flush=True)
    sys.exit(7)
if mode == "exit-zero":
    sys.exit(0)
if mode == "exit-two":
    saved_metadata()
    sys.exit(2)
if mode == "spam":
    print("X" * 120000, flush=True)
    for i in range(2000):
        print("line " + str(i), flush=True)
    sys.exit(7)
def stop(sig, frame):
    with (output / "signals.txt").open("a") as stream:
        stream.write(str(sig) + "\n")
    if mode == "ignore":
        return
    if mode != "missing-metadata":
        saved_metadata()
    sys.exit(2 if mode.startswith("foxy-") else 0)
signal.signal(signal.SIGINT, stop)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, stop)
(output / "ready").touch()
print("Recording...", flush=True)
while True:
    time.sleep(0.02)
'''


@pytest.fixture
def fake_ros2(tmp_path):
    script = tmp_path / "fake_ros2.py"
    script.write_text(FAKE_ROS2, encoding="utf-8")
    return script


def make_recorder(script, mode="normal", **kwargs):
    return Recorder(ros2_command=(sys.executable, "-u", str(script), mode), **kwargs)


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for the fake recorder")


def cleanup(recorder):
    # Tests own synthetic processes only. Production Recorder never force-kills.
    if recorder.is_active:
        if os.name == "posix":
            os.killpg(recorder.pid, signal.SIGKILL)
        else:
            recorder._process.kill()
        recorder._process.wait(timeout=5)
    recorder.poll()


@pytest.mark.parametrize("name", ["", ".", "..", "../bag", "a/b", "a\\b", "a;whoami", "a\n", "-a", "CON", "bag."])
def test_reject_unsafe_bag_names(tmp_path, name):
    recorder = Recorder()
    with pytest.raises(ValueError):
        recorder.start(["/camera/image"], tmp_path, name)
    assert recorder.state == "idle"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("topics", [[], ["--all"], ["/a;touch"], ["relative"], ["/a//b"], ["/a/"], ["/123"]])
def test_reject_invalid_topics(tmp_path, topics):
    with pytest.raises(ValueError):
        Recorder().start(topics, tmp_path, "bag")
    assert list(tmp_path.iterdir()) == []


def test_existing_output_is_preserved(tmp_path):
    output = tmp_path / "bag"
    output.mkdir()
    marker = output / "precious.db3"
    marker.write_text("existing")
    with pytest.raises(FileExistsError):
        Recorder().start(["/tf"], tmp_path, "bag")
    assert marker.read_text() == "existing"


def test_missing_executable_has_actionable_failure(tmp_path):
    recorder = Recorder(ros2_command=(str(tmp_path / "missing-executable"),))
    with pytest.raises(RecorderError, match="Could not start"):
        recorder.start(["/tf"], tmp_path, "bag")
    assert recorder.state == "failed"
    assert not recorder.is_active


@pytest.mark.parametrize("kind", ["missing", "directory", "empty"])
def test_invalid_qos_override_is_rejected_before_creating_output(tmp_path, kind):
    profile = tmp_path / "qos.yaml"
    if kind == "directory":
        profile.mkdir()
    elif kind == "empty":
        profile.touch()
    recorder = Recorder(qos_overrides=profile)
    with pytest.raises(ValueError, match="QoS override"):
        recorder.start(["/imu"], tmp_path / "new-output-parent", "bag")
    assert recorder.state == "idle"
    assert not recorder.is_active
    assert not (tmp_path / "new-output-parent").exists()


def test_unreadable_qos_override_is_rejected(tmp_path, monkeypatch):
    profile = tmp_path / "qos.yaml"
    profile.write_text("/imu: {history: keep_last, depth: 1000}\n")
    original_open = Path.open

    def restricted_open(path, *args, **kwargs):
        if path == profile:
            raise PermissionError("access denied by ACL")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", restricted_open)
    recorder = Recorder(qos_overrides=profile)
    with pytest.raises(ValueError, match="Cannot read QoS override"):
        recorder.start(["/imu"], tmp_path / "new-output-parent", "bag")
    assert not (tmp_path / "new-output-parent").exists()


@pytest.mark.parametrize("use_override", [False, True])
def test_qos_override_is_explicit_and_passed_as_one_argument(tmp_path, fake_ros2, use_override):
    profile = tmp_path / "profile with spaces.yaml"
    content = b"/imu:\n  history: keep_last\n  depth: 1000\n  reliability: reliable\n  durability: volatile\n"
    profile.write_bytes(content)
    recorder = make_recorder(fake_ros2, "crash", qos_overrides=profile if use_override else None)
    output = recorder.start(["/imu"], tmp_path, "bag")
    try:
        wait_until(lambda: recorder.poll() == "failed")
        arguments = json.loads((output / "arguments.json").read_text())
        if use_override:
            flag_index = arguments.index("--qos-profile-overrides-path")
            assert arguments[flag_index + 1] == str(profile.resolve())
        else:
            assert "--qos-profile-overrides-path" not in arguments
        assert arguments[-1] == "/imu"
        assert profile.read_bytes() == content
    finally:
        cleanup(recorder)


@pytest.mark.parametrize("mode,expected", [("crash", "exit code 7"), ("exit-zero", "exited unexpectedly"), ("exit-two", "exit code 2")])
def test_unexpected_process_exit_is_failure(tmp_path, fake_ros2, mode, expected):
    recorder = make_recorder(fake_ros2, mode)
    recorder.start(["/tf"], tmp_path, "bag")
    try:
        wait_until(lambda: recorder.poll() == "failed")
        assert expected in recorder.error
        if mode != "exit-two":
            assert "metadata.yaml" in recorder.error
        assert not recorder.is_active
    finally:
        cleanup(recorder)


def test_bounded_log_reader_does_not_deadlock_on_long_lines(tmp_path, fake_ros2):
    recorder = make_recorder(fake_ros2, "spam", max_log_lines=10)
    recorder.start(["/tf"], tmp_path, "bag")
    try:
        wait_until(lambda: recorder.poll() == "failed")
        recorder._reader.join(timeout=2)
        lines = recorder.drain_logs()
        assert len(lines) <= 10
        assert all(len(line) <= 4096 for line in lines)
        assert lines[-1] == "line 1999"
        assert recorder.drain_logs() == []
    finally:
        cleanup(recorder)


@pytest.mark.skipif(os.name != "posix", reason="Linux/ROS process-group SIGINT integration")
def test_record_and_gracefully_stop_with_metadata(tmp_path, fake_ros2):
    recorder = make_recorder(fake_ros2)
    output = recorder.start(["/camera/image", "/tf", "/tf"], tmp_path / "spaces are safe", "试验_01")
    try:
        wait_until(lambda: (output / "ready").exists())
        assert recorder.is_active
        assert recorder.started_at is not None
        assert (output / "stdin_is_tty").read_text() == "True"
        arguments = json.loads((output / "arguments.json").read_text())
        assert arguments[-2:] == ["/camera/image", "/tf"]
        assert "--include-hidden-topics" in arguments
        assert arguments[arguments.index("--output") + 1] == str(output)
        with pytest.raises(RecorderError, match="already active"):
            recorder.start(["/tf"], tmp_path, "second")
        recorder.request_stop()
        recorder.request_stop()
        wait_until(lambda: recorder.poll() == "finished")
        assert not recorder.is_active
        assert recorder.returncode == 0
        assert recorder.error is None
        assert (output / "signals.txt").read_text().splitlines() == [str(int(signal.SIGINT))]
    finally:
        cleanup(recorder)


@pytest.mark.skipif(os.name != "posix", reason="Linux/ROS process-group SIGINT integration")
def test_clean_exit_without_metadata_is_not_finalized(tmp_path, fake_ros2):
    recorder = make_recorder(fake_ros2, "missing-metadata")
    output = recorder.start(["/tf"], tmp_path, "bag")
    try:
        wait_until(lambda: (output / "ready").exists())
        recorder.request_stop()
        wait_until(lambda: recorder.poll() == "failed")
        assert "finalization is unverified" in recorder.error
    finally:
        cleanup(recorder)


@pytest.mark.skipif(os.name != "posix", reason="Linux/ROS process-group SIGINT integration")
def test_stop_timeout_keeps_process_alive_and_sends_sigint_only_once(tmp_path, fake_ros2):
    recorder = make_recorder(fake_ros2, "ignore", stop_timeout=0.05)
    output = recorder.start(["/tf"], tmp_path, "bag")
    try:
        wait_until(lambda: (output / "ready").exists())
        recorder.request_stop()
        wait_until(lambda: recorder.poll() == "stopping" and recorder.error is not None)
        recorder.request_stop()
        assert recorder.is_active
        assert "No forced kill" in recorder.error
        assert (output / "signals.txt").read_text().splitlines() == [str(int(signal.SIGINT))]
    finally:
        cleanup(recorder)


def test_exited_launcher_waits_for_its_process_group(tmp_path, monkeypatch):
    recorder = Recorder()
    recorder._process = Mock()
    recorder._process.poll.return_value = -signal.SIGINT
    recorder._process.pid = 12345
    recorder._stop_requested_at = time.monotonic()
    recorder.output_path = tmp_path
    (tmp_path / "metadata.yaml").write_text("rosbag2_bagfile_information: {}")
    monkeypatch.setattr(recorder, "_group_alive", lambda: True)
    assert recorder.poll() == "stopping"
    assert recorder.is_active
    monkeypatch.setattr(recorder, "_group_alive", lambda: False)
    assert recorder.poll() == "finished"
    assert not recorder.is_active


def test_finished_requires_success_even_when_metadata_exists(tmp_path, monkeypatch):
    recorder = Recorder()
    recorder._process = Mock()
    recorder._process.poll.return_value = 9
    recorder._stop_requested_at = time.monotonic()
    recorder.output_path = tmp_path
    (tmp_path / "metadata.yaml").write_text("rosbag2_bagfile_information: {}")
    monkeypatch.setattr(recorder, "_group_alive", lambda: False)
    assert recorder.poll() == "failed"
    assert "exit code 9" in recorder.error


@pytest.mark.skipif(os.name != "posix", reason="ROS process-group SIGINT integration")
def test_foxy_requested_stop_exit_two_validates_sqlite(tmp_path, fake_ros2):
    recorder = make_recorder(fake_ros2, "foxy-stop")
    output = recorder.start(["/tf"], tmp_path, "bag")
    try:
        wait_until(lambda: (output / "ready").exists())
        recorder.request_stop()
        wait_until(lambda: recorder.poll() == "finished")
        assert recorder.returncode == 2
        assert recorder.error is None
        assert not recorder.is_active
        args = json.loads((output / "arguments.json").read_text())
        assert args[args.index("--max-cache-size") + 1] == "0"
        assert "cache disabled" in recorder.settings_notice
    finally:
        cleanup(recorder)


@pytest.mark.skipif(os.name != "posix", reason="ROS process-group SIGINT integration")
def test_foxy_metadata_count_with_empty_database_is_not_saved(tmp_path, fake_ros2):
    recorder = make_recorder(fake_ros2, "foxy-bad-count")
    output = recorder.start(["/camera/image"], tmp_path, "bag")
    try:
        wait_until(lambda: (output / "ready").exists())
        recorder.request_stop()
        wait_until(lambda: recorder.poll() == "failed")
        assert recorder.returncode == 2
        assert "count" in recorder.error.lower()
    finally:
        cleanup(recorder)


def test_sqlite_validation_does_not_block_poll_or_allow_new_recording(tmp_path, monkeypatch):
    import threading
    from neobag import recorder as module
    entered, release = threading.Event(), threading.Event()
    def validate(path):
        entered.set()
        assert release.wait(timeout=5)
        return None
    monkeypatch.setattr(module, "validate_sqlite_metadata", validate)
    recorder = Recorder()
    recorder._process = Mock()
    recorder._process.poll.return_value = 2
    recorder._stop_requested_at = time.monotonic()
    recorder.output_path = tmp_path
    recorder.resolved_storage = "sqlite3"
    (tmp_path / "metadata.yaml").write_text("finalized")
    monkeypatch.setattr(recorder, "_group_alive", lambda: False)
    try:
        start = time.monotonic()
        assert recorder.poll() == "stopping"
        assert time.monotonic() - start < 0.5
        assert entered.wait(timeout=2)
        assert recorder.is_active
        with pytest.raises(RecorderError, match="already active"):
            recorder.start(["/tf"], tmp_path, "another")
    finally:
        release.set()
        wait_until(lambda: recorder.poll() == "finished")
    assert not recorder.is_active

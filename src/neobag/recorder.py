"""Own one rosbag recorder process group without blocking the terminal UI.

The lifecycle methods are called by the UI thread. Only the bounded log reader
runs in a background thread. A finished state means graceful exit plus written
metadata; it does not certify message completeness or recording frequency.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future
import math
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import tempfile
import time
from typing import IO, Sequence

from .settings import resolve_settings, buffered_qos, CYCLONE_RECEIVER_CONFIG
from .finalization import validate_sqlite_metadata


class RecorderError(RuntimeError):
    """A recorder could not be launched or is already running."""


class Recorder:
    """Launch ``ros2 bag record`` and request a graceful, nonblocking stop.

    ``poll()`` advances state and returns its string value. A slow stop remains
    active and exposes ``error`` after ``stop_timeout`` seconds; it is never
    escalated to SIGKILL. The caller must not exit while ``is_active`` is true.
    ``ros2_command`` can select a ROS environment launcher or a test fixture.
    ``qos_overrides`` opts into ROS's explicit per-topic QoS YAML. An override
    replaces ROS's adaptive profile for that topic; omitted reliability and
    durability fields do not retain the publisher-adapted values.
    """

    def __init__(
        self,
        *,
        ros2_command: Sequence[str] = ("ros2",),
        stop_timeout: float = 30.0,
        max_log_lines: int = 250,
        qos_overrides: Path | None = None,
        rmw: str = "auto",
        cache_mib: int | None = None,
        buffer_qos: bool = True,
    ) -> None:
        if isinstance(ros2_command, str) or not ros2_command:
            raise ValueError("ros2_command must be a nonempty sequence of arguments")
        if not math.isfinite(stop_timeout) or stop_timeout <= 0:
            raise ValueError("stop_timeout must be positive and finite")
        if max_log_lines < 1:
            raise ValueError("max_log_lines must be positive")
        self._command = tuple(ros2_command)
        self._stop_timeout = stop_timeout
        self._max_log_lines = max_log_lines
        self.qos_overrides = Path(qos_overrides).expanduser() if qos_overrides is not None else None
        self.rmw = rmw
        self.cache_mib = cache_mib
        self.buffer_qos = buffer_qos
        self._qos_directory = None
        self._generated_qos = None
        self.settings_notice = ""
        self.configuration_warning = ""
        self.resolved_storage = ""
        self._process: subprocess.Popen[str] | None = None
        self._pty_master: int | None = None
        self._logs: deque[str] = deque(maxlen=max_log_lines)
        self._log_lock = threading.Lock()
        self._stop_requested_at: float | None = None
        self._reader: threading.Thread | None = None
        self._finalized = False
        self.state = "idle"
        self.output_path: Path | None = None
        self.error: str | None = None
        self.returncode: int | None = None
        self.started_at: float | None = None
        self.transport_lost = 0
        self._validation_future: Future | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def is_active(self) -> bool:
        """Include owned descendants if an environment launcher exits first."""
        if self._process is None or self._finalized:
            return False
        # A requested stop is not complete until poll() validates saved output.
        if self._stop_requested_at is not None:
            return True
        if self._process.poll() is None:
            return True
        return self._group_alive()

    def _group_alive(self) -> bool:
        if os.name != "posix" or self._process is None:
            return False
        try:
            os.killpg(self._process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Do not silently abandon a process group whose status is unknown.
            return True
        return True

    @staticmethod
    def _validate_name(bag_name: str) -> None:
        # Unicode word characters permit useful non-English bag names. Path
        # separators, shell syntax, control characters and leading dots do not.
        if not re.fullmatch(r"[\w][\w.-]{0,127}", bag_name) or bag_name.endswith("."):
            raise ValueError(
                "Bag name must be 1-128 letters, digits, underscores, dots or "
                "hyphens; start with a letter, digit or underscore."
            )
        if bag_name.split(".")[0].upper() in {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }:
            raise ValueError("Bag name is a reserved filesystem name")

    def start(
        self,
        topics: list[str],
        output_parent: Path,
        bag_name: str,
        storage: str = "auto",
    ) -> Path:
        """Validate and launch; the recorder itself creates the bag directory."""
        self.poll()
        if self.is_active:
            raise RecorderError("A recording is already active; stop it first")
        self._validate_name(bag_name)
        if not topics:
            raise ValueError("Select at least one topic")
        if any(not re.fullmatch(r"/(?:[A-Za-z_][A-Za-z0-9_]*)(?:/[A-Za-z_][A-Za-z0-9_]*)*", t)
               for t in topics):
            raise ValueError("Topics must be fully qualified ROS topic names")
        if not re.fullmatch(r"[A-Za-z0-9_]+", storage):
            raise ValueError("Invalid rosbag storage identifier")
        qos_path: Path | None = None
        if self.qos_overrides is not None:
            try:
                qos_path = self.qos_overrides.resolve(strict=True)
                if not qos_path.is_file():
                    raise ValueError(f"QoS override must be a regular file: {qos_path}")
                # Check actual readability, not just mode bits (ACLs/root can
                # differ). ROS validates YAML and policy values on launch.
                with qos_path.open("rb") as profile:
                    if not profile.read(1):
                        raise ValueError(f"QoS override file is empty: {qos_path}")
            except OSError as exc:
                raise ValueError(f"Cannot read QoS override file {self.qos_overrides}: {exc}") from exc
        parent = Path(output_parent).expanduser().resolve()
        output = parent / bag_name
        if output.exists() or output.is_symlink():
            raise FileExistsError(f"Bag output already exists: {output}")
        parent.mkdir(parents=True, exist_ok=True)

        storage, environment, cache = resolve_settings(self._command, storage, self.rmw, self.cache_mib)
        self.resolved_storage = storage
        cache_notice = f"cache {cache / 2**20:.0f} MiB × 2" if cache else "cache disabled (direct writes)"
        self.settings_notice = f"{storage} · {cache_notice} · {environment.get('RMW_IMPLEMENTATION', 'inherited RMW')}"
        if environment.get("CYCLONEDDS_URI") == CYCLONE_RECEIVER_CONFIG:
            self.settings_notice += " (async RX)"
        self.configuration_warning = ""
        self._cleanup_qos()
        if qos_path is None and self.buffer_qos:
            try:
                profiles = buffered_qos(self._command, topics, environment)
                if profiles:
                    self._qos_directory = tempfile.TemporaryDirectory(prefix="neobag-qos-")
                    qos_path = Path(self._qos_directory.name) / "receiver-qos.yaml"
                    # JSON is a YAML subset accepted by rosbag's YAML loader.
                    self._generated_qos = json.dumps(profiles, indent=2)
                    qos_path.write_text(self._generated_qos, encoding="utf-8")
                self.settings_notice += f" · buffered QoS: {len(profiles)} topics"
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                self._cleanup_qos()
                qos_path = None
                self.configuration_warning = f"Buffered receiver QoS unavailable ({type(exc).__name__}); using ROS adaptive QoS."
                self.settings_notice += " · " + self.configuration_warning

        self.output_path = output
        self.error = None
        self.returncode = None
        self.started_at = None
        self._stop_requested_at = None
        self._finalized = False
        self._process = None
        self._logs = deque(maxlen=self._max_log_lines)
        self.transport_lost = 0
        self._validation_future = None
        arguments = [
            *self._command, "bag", "record", "--storage", storage,
            "--output", str(output), "--include-hidden-topics",
            "--max-cache-size", str(cache),
        ]
        if qos_path is not None:
            arguments.extend(("--qos-profile-overrides-path", str(qos_path)))
        arguments.extend(dict.fromkeys(topics))
        kwargs: dict = {}
        slave: int | None = None
        try:
            if os.name == "posix":
                # Humble's Recorder has no --disable-keyboard-controls option
                # and constructs a keyboard handler unconditionally. Give it a
                # private terminal so it cannot consume the TUI's keystrokes.
                import pty

                self._pty_master, slave = pty.openpty()
                kwargs["stdin"] = slave
                kwargs["start_new_session"] = True
            else:
                kwargs["stdin"] = subprocess.DEVNULL
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            self._process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                env=environment,
                **kwargs,
            )
        except OSError as exc:
            self._close_pty()
            self._cleanup_qos()
            self.state = "failed"
            self.error = f"Could not start ros2 bag record: {exc}"
            raise RecorderError(self.error) from exc
        finally:
            if slave is not None:
                os.close(slave)
        self.started_at = time.monotonic()
        self.state = "recording"
        assert self._process.stdout is not None
        self._reader = threading.Thread(
            target=self._read_logs,
            args=(self._process.stdout, self._logs),
            name="neobag-recorder-log",
            daemon=True,
        )
        self._reader.start()
        return output

    def _read_logs(self, stream: IO[str], destination: deque[str]) -> None:
        try:
            # A malicious or malformed no-newline log cannot grow without limit.
            while chunk := stream.readline(4096):
                with self._log_lock:
                    destination.append(chunk.rstrip("\r\n"))
                    match = re.search(r"Number of messages lost on the transport layer:\s*(\d+)", chunk)
                    if match:
                        self.transport_lost = max(self.transport_lost, int(match[1]))
        except (OSError, ValueError) as exc:
            with self._log_lock:
                destination.append(f"Recorder log stream closed: {exc}")
        finally:
            stream.close()

    def drain_logs(self) -> list[str]:
        with self._log_lock:
            result = list(self._logs)
            self._logs.clear()
        return result

    def request_stop(self) -> None:
        """Send SIGINT once, only to this recorder's owned process group."""
        self.poll()
        if not self.is_active or self._stop_requested_at is not None:
            return
        self._stop_requested_at = time.monotonic()
        self.state = "stopping"
        self.error = None
        assert self._process is not None
        try:
            if os.name == "posix":
                os.killpg(self._process.pid, signal.SIGINT)
            else:
                self._process.send_signal(signal.CTRL_BREAK_EVENT)
        except ProcessLookupError:
            self.poll()
        except OSError as exc:
            self.error = f"Could not signal the recorder to stop: {exc}"

    def poll(self) -> str:
        """Advance state without waiting for process exit or disk flushing."""
        if self._process is None or self._finalized:
            return self.state
        self.returncode = self._process.poll()
        if self.returncode is None or self._group_alive():
            if self._stop_requested_at is not None:
                self.state = "stopping"
                if time.monotonic() - self._stop_requested_at > self._stop_timeout:
                    self.error = (
                        f"Stop is taking more than {self._stop_timeout:g}s. "
                        "Recorder processes are still active; waiting for a safe flush. "
                        "Do not close this terminal or power off. No forced kill was sent."
                    )
            elif self.returncode is not None:
                self.state = "failed"
                self.error = (
                    f"Recorder launcher exited ({self.returncode}) but child processes "
                    "are still running. Use Stop to close them safely."
                )
            return self.state

        metadata = self.output_path / "metadata.yaml" if self.output_path else None
        try:
            has_metadata = bool(metadata and metadata.is_file() and metadata.stat().st_size > 0)
        except OSError:
            has_metadata = False
        requested_stop = self._stop_requested_at is not None
        # Foxy's ros2cli translates KeyboardInterrupt to signal.SIGINT == 2.
        # Accept that code only for our own stop and validated SQLite output.
        acceptable_exit = self.returncode == 0 or (
            requested_stop and self.returncode in (-signal.SIGINT, 130, int(signal.SIGINT))
        )
        validation_error = None
        if acceptable_exit and has_metadata and requested_stop and (
                self.resolved_storage == "sqlite3" or self.returncode == int(signal.SIGINT)):
            if self._validation_future is None:
                self._validation_future = Future()
                future = self._validation_future
                output = self.output_path

                def validate():
                    try:
                        future.set_result(validate_sqlite_metadata(output))
                    except Exception as exc:
                        future.set_result(f"Saved SQLite validation failed: {exc}")

                threading.Thread(target=validate, name="neobag-finalize", daemon=True).start()
            if not self._validation_future.done():
                self.state = "stopping"
                return self.state
            validation_error = self._validation_future.result()

        self._finalized = True
        self._close_pty()
        if self._generated_qos is not None and self.output_path and self.output_path.is_dir():
            try:
                with (self.output_path / "neobag-receiver-qos.yaml").open("x", encoding="utf-8") as profile:
                    profile.write(self._generated_qos)
            except OSError as exc:
                with self._log_lock:
                    self._logs.append(f"Could not retain receiver QoS settings: {exc}")
        self._cleanup_qos()
        if acceptable_exit and has_metadata and requested_stop and validation_error is None:
            self.state = "finished"
            self.error = None
        else:
            self.state = "failed"
            reasons = []
            if self._stop_requested_at is None:
                reasons.append("recorder exited unexpectedly")
            if not acceptable_exit:
                reasons.append(f"exit code {self.returncode}")
            if not has_metadata:
                reasons.append("metadata.yaml is missing or empty; bag finalization is unverified")
            if validation_error:
                reasons.append(validation_error)
            self.error = "; ".join(reasons) + ". Inspect the recorder log and bag output."
        return self.state

    def _close_pty(self) -> None:
        if self._pty_master is not None:
            os.close(self._pty_master)
            self._pty_master = None

    def _cleanup_qos(self) -> None:
        if self._qos_directory is not None:
            self._qos_directory.cleanup()
            self._qos_directory = None
        self._generated_qos = None

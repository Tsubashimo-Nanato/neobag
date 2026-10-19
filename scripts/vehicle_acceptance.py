"""Opt-in acceptance through the real, visible neobag terminal interface.

Run with the installed neobag Python environment and sourced ROS environment::

    python scripts/vehicle_acceptance.py --output '/path/to/bags' \
        --evidence '/path/to/new-evidence-directory'

Uses existing publishers only. Does not launch vehicle nodes, publish commands,
replay bags, patch recorder settings, or force-kill recording processes. The UI
stays open after completion or failure; use its normal Quit action when done.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

import neobag
from neobag.app import NeoBagApp
from neobag.finalization import validate_sqlite_metadata
from neobag.resources import ResourceMonitor
from neobag.topics import TopicDiscovery
from textual.events import Mount
from textual.widgets import Button
import yaml


DEFAULT_TOPICS = (
    "/aiformula_sensing/zed_node/left_image/undistorted",
    "/aiformula_control/game_pad/cmd_vel",
)
IMAGE_TYPES = {"sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage"}


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class TracedDiscovery(TopicDiscovery):
    def __init__(self, evidence: Path):
        super().__init__()
        self.evidence = evidence
        self.calls = 0
        self.completed = 0
        self._trace_lock = threading.Lock()

    def _trace(self, data: dict) -> None:
        with (self.evidence / "discovery.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(data) + "\n")

    def scan(self):
        with self._trace_lock:
            self.calls += 1
            number = self.calls
            self._trace({"event": "start", "scan": number, "monotonic": time.monotonic()})
        result = {"event": "finish", "scan": number}
        try:
            topics = super().scan()
            result["topics"] = [asdict(topic) for topic in topics]
            return topics
        except Exception as exc:
            result["error"] = str(exc)
            raise
        finally:
            with self._trace_lock:
                self.completed += 1
                result["monotonic"] = time.monotonic()
                self._trace(result)


class LoggedResourceMonitor(ResourceMonitor):
    def __init__(self, evidence: Path):
        super().__init__()
        self.evidence = evidence
        self.phase = "discovery"
        self.samples: list[dict] = []
        self._log_lock = threading.Lock()

    def set_phase(self, phase: str) -> None:
        with self._log_lock:
            self.phase = phase

    def sample(self, parent):
        snapshot = super().sample(parent)
        with self._log_lock:
            if self.phase not in {"complete", "failed"}:
                row = {"monotonic": time.monotonic(), "phase": self.phase, **asdict(snapshot)}
                with (self.evidence / "resources.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row) + "\n")
                self.samples.append(row)
        return snapshot

    def summary(self) -> dict:
        with self._log_lock:
            rows = list(self.samples)
        result = {}
        for phase in dict.fromkeys(row["phase"] for row in rows):
            samples = [row for row in rows if row["phase"] == phase]
            measurements = {"samples": len(samples)}
            for key in ("cpu_percent", "gpu_percent", "ram_percent", "disk_free_bytes"):
                values = [row[key] for row in samples if row[key] is not None]
                measurements[key] = ({"mean": sum(values) / len(values), "min": min(values),
                                      "max": max(values)} if values else None)
            result[phase] = measurements
        return result


class EvidenceLog(deque):
    """Retain the normal bounded UI log and persist every appended line."""
    def __init__(self, path: Path):
        super().__init__(maxlen=400)
        self.path = path

    def append(self, line):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(str(line) + "\n")
        super().append(line)

    def extend(self, lines):
        for line in lines:
            self.append(line)


def inspect_saved_bag(output: Path, requested: set[str], image_topics: set[str]) -> dict:
    """Read finalized metadata and actual SQLite rows without modifying a bag."""
    error = validate_sqlite_metadata(output)
    if error:
        raise RuntimeError(error)
    information = yaml.safe_load((output / "metadata.yaml").read_text(encoding="utf-8"))[
        "rosbag2_bagfile_information"
    ]
    topics = {name: {"messages": 0, "payload_bytes": 0, "first_timestamp": None,
                     "last_timestamp": None} for name in requested}
    databases = []
    for relative in information["relative_file_paths"]:
        database = (output / relative).resolve(strict=True)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT topics.name, topics.type, COUNT(messages.id), "
                "COALESCE(SUM(LENGTH(messages.data)), 0), "
                "MIN(messages.timestamp), MAX(messages.timestamp) "
                "FROM topics LEFT JOIN messages ON messages.topic_id = topics.id "
                "GROUP BY topics.name, topics.type"
            ).fetchall()
        databases.append({"path": str(database), "bytes": database.stat().st_size})
        for name, message_type, count, payload_bytes, first, last in rows:
            topic = topics.setdefault(name, {"messages": 0, "payload_bytes": 0,
                                            "first_timestamp": None, "last_timestamp": None})
            topic["type"] = message_type
            topic["messages"] += count
            topic["payload_bytes"] += payload_bytes
            if first is not None:
                previous = topic["first_timestamp"]
                topic["first_timestamp"] = first if previous is None else min(previous, first)
                previous = topic["last_timestamp"]
                topic["last_timestamp"] = last if previous is None else max(previous, last)
    for name in image_topics:
        if topics[name]["messages"] <= 0:
            raise RuntimeError(f"No real image messages were saved for {name}")
    return {"metadata_validation": "passed", "metadata_message_count": information["message_count"],
            "topics": topics, "databases": databases,
            "bag_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file()),
            "command_topics_may_be_empty": True}


class VehicleAcceptanceApp(NeoBagApp):
    def __init__(self, *, output: Path, name: str, evidence: Path, topics: set[str], seconds: float):
        self.evidence = evidence
        self.requested_topics = topics
        self.recording_seconds = seconds
        super().__init__(output_parent=output, bag_name=name,
                         discovery=TracedDiscovery(evidence), monitor=LoggedResourceMonitor(evidence))
        self.lines = EvidenceLog(evidence / "recorder.log")
        self.result = {"phase": "initializing", "ui": "visible Textual terminal; production button events",
                       "ui_pid": os.getpid(), "neobag_version": version("neobag"),
                       "neobag_module": neobag.__file__, "python": sys.version,
                       "topics_requested": sorted(topics), "output": str(output / name),
                       "recording_seconds_requested": seconds,
                       "ros_environment": {key: os.environ.get(key) for key in
                                           ("ROS_DISTRO", "ROS_DOMAIN_ID", "RMW_IMPLEMENTATION")}}

    def on_mount(self, event: Mount) -> None:
        # Textual otherwise dispatches the base handler again after this one.
        event.prevent_default()
        super().on_mount()
        self.run_worker(self.acceptance(), exit_on_error=False)

    def record_details(self) -> dict:
        process = self.recorder._process
        return {"recorder_state": self.recorder.state, "recorder_returncode": self.recorder.returncode,
                "recorder_error": self.recorder.error, "recorder_pid": self.recorder.pid,
                "record_command": process.args if process is not None else None,
                "resolved_storage": self.recorder.resolved_storage,
                "settings_notice": self.recorder.settings_notice,
                "transport_lost_reported": self.recorder.transport_lost,
                "discovery_scans": self.discovery.calls}

    def set_phase(self, phase: str, **details) -> None:
        self.monitor.set_phase(phase)
        self.result.update(phase=phase, updated_utc=datetime.now(timezone.utc).isoformat(),
                           **self.record_details(), **details)
        write_json(self.evidence / "status.json", self.result)

    async def wait_for_scan(self, count: int) -> None:
        deadline = time.monotonic() + 35
        while self.discovery.completed < count or self._scanning:
            if time.monotonic() > deadline:
                raise RuntimeError("Topic discovery did not finish within 35 seconds")
            await asyncio.sleep(0.1)
        if self.discovery_error:
            raise RuntimeError(self.discovery_error)

    def press(self, selector: str) -> None:
        button = self.query_one(selector, Button)
        if button.disabled:
            raise RuntimeError(f"Required UI button is disabled: {selector}; {self.notice}")
        button.press()

    async def wait_for_started(self) -> None:
        deadline = time.monotonic() + 60
        while True:
            # Advance the lifecycle before checking it, including early exits.
            self.poll_recorder()
            if not self._starting and self.recorder.state == "recording" and self.recorder.is_active:
                return
            if self.recorder.state == "failed" or self.notice.startswith("Cannot start:"):
                raise RuntimeError(self.recorder.error or self.notice)
            if time.monotonic() > deadline:
                raise RuntimeError("The Record button did not finish preparing within 60 seconds")
            await asyncio.sleep(0.1)

    async def wait_for_saved(self) -> None:
        started = time.monotonic()
        reported = started
        while True:
            # is_active alone can change before metadata validation has run.
            self.poll_recorder()
            if not self.recorder.is_active and self.recorder.state in {"finished", "failed"}:
                return
            now = time.monotonic()
            if now - started > 30 and now - reported >= 5:
                self.set_phase("saving_wait", saving_seconds=round(now - started, 1),
                               waiting="Recorder is still saving; waiting without forced termination.")
                reported = now
            await asyncio.sleep(0.2)

    async def acceptance(self) -> None:
        self.set_phase("discovery")
        try:
            await self.wait_for_scan(1)
            discovered = {topic.name: topic for topic in self.topics}
            missing = self.requested_topics - discovered.keys()
            if missing:
                raise RuntimeError("Requested topics are absent from the real graph: " + ", ".join(sorted(missing)))
            images = {name for name in self.requested_topics if set(discovered[name].types) & IMAGE_TYPES}
            if not images:
                raise RuntimeError("Select at least one real sensor_msgs Image or CompressedImage topic")
            self._pending_profile = None
            self.selected = set(self.requested_topics)
            self.rebuild_topics()
            await asyncio.sleep(0.2)
            self.save_screenshot(filename="selected.svg", path=str(self.evidence))
            self.set_phase("settling")
            await asyncio.sleep(1.2)
            self.set_phase("baseline")
            await asyncio.sleep(12)
            if self.discovery.calls != 1:
                raise RuntimeError("Idle UI performed automatic topic discovery")

            self.set_phase("manual_refresh")
            self.press("#refresh")
            await self.wait_for_scan(2)
            if self.discovery.calls != 2 or self.selected != self.requested_topics:
                raise RuntimeError("Manual Refresh did not scan once and preserve the exact selection")

            self.set_phase("preparing")
            self.press("#start")
            await self.wait_for_started()
            self.set_phase("recording", recording_started_monotonic=self.recorder.started_at)
            self.save_screenshot(filename="recording.svg", path=str(self.evidence))
            deadline = self.recorder.started_at + self.recording_seconds
            while time.monotonic() < deadline:
                self.poll_recorder()
                if not self.recorder.is_active or self.recorder.state != "recording":
                    raise RuntimeError(self.recorder.error or "Recorder stopped before the requested duration")
                await asyncio.sleep(0.2)
            self.set_phase("saving", recording_seconds_actual=time.monotonic() - self.recorder.started_at)
            self.press("#stop")
            await self.wait_for_saved()
            if self.recorder.state != "finished":
                raise RuntimeError(self.recorder.error or "Recorder did not finalize successfully")
            self.set_phase("validating")
            self.result["saved_bag"] = await asyncio.to_thread(
                inspect_saved_bag, self.recorder.output_path, self.requested_topics, images,
            )
            self.save_screenshot(filename="saved.svg", path=str(self.evidence))
            self.set_phase("after")
            await asyncio.sleep(12)
            if self.discovery.calls != 2:
                raise RuntimeError("UI automatically rescanned topics after recording")
            self.result["resource_summary"] = self.monitor.summary()
            self.result["passed"] = True
            self.set_phase("complete")
            write_json(self.evidence / "result.json", self.result)
            self.notice = "Acceptance passed · real bag saved and validated. Evidence: " + str(self.evidence)
            self.update_summary()
        except Exception as exc:
            self.result.update(passed=False, error=str(exc))
            # An in-flight Popen must finish before deciding whether Stop is needed.
            while self._starting:
                self.set_phase("waiting_for_start", waiting="Preparing still active; waiting before cleanup.")
                await asyncio.sleep(1)
            self.poll_recorder()
            if self.recorder.is_active:
                self.set_phase("saving_after_failure")
                self.press("#stop")
                await self.wait_for_saved()
            self.result["resource_summary"] = self.monitor.summary()
            self.set_phase("failed")
            write_json(self.evidence / "result.json", self.result)
            self.notice = "Acceptance failed: " + str(exc) + ". Evidence: " + str(self.evidence)
            self.update_summary()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="Parent directory for the new bag")
    parser.add_argument("--evidence", required=True, type=Path, help="New directory for logs, results and screenshots")
    parser.add_argument("--name", default=datetime.now(timezone.utc).strftime("neobag_acceptance_%Y%m%d_%H%M%SZ"))
    parser.add_argument("--seconds", type=float, default=30, help="Recording duration; default: 30 seconds")
    parser.add_argument("--topic", action="append", help="Repeat for each topic; defaults to left undistorted image + cmd_vel")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be positive and finite")
    output = args.output.expanduser().resolve()
    evidence = args.evidence.expanduser().resolve()
    if (output / args.name).exists():
        parser.error("The requested bag already exists; choose a new --name")
    evidence.mkdir(parents=True, exist_ok=False)
    app = VehicleAcceptanceApp(output=output, name=args.name, evidence=evidence,
                               topics=set(args.topic or DEFAULT_TOPICS), seconds=args.seconds)
    try:
        app.run()
    finally:
        app.finish_cleanup()


if __name__ == "__main__":
    main()

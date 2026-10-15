"""Opt-in full-bag replay/capture acceptance using real ROS and the neobag UI.

Requires Linux, a sourced ROS environment containing the bag's message types,
and test dependencies. Always uses a dedicated localhost-only ROS domain.
Original bag files are read-only. Output must be a new directory.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import yaml
from neobag.app import NeoBagApp
from neobag.recorder import Recorder
from neobag.resources import ResourceMonitor
from neobag.topics import TopicDiscovery
from textual.widgets import Static

from compare_bags import compare_bags


class CaptureRecorder(Recorder):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_logs = []

    def drain_logs(self):
        lines = super().drain_logs()
        self.captured_logs.extend(lines)
        return lines


class CaptureApp(NeoBagApp):
    def __init__(self, evidence, **kwargs):
        super().__init__(**kwargs)
        self.evidence = evidence
        self.samples = []
        self.pressure_shown = False
        self.pressure_cleared = False
        self.pressure_start = None
        self.pressure_end = None
        self.pressure_capture_monotonic = None
        self.recovery_capture_monotonic = None
        self._high_cpu_samples = 0

    def show_resources(self, snapshot):
        super().show_resources(snapshot)
        sample = {"monotonic": time.monotonic(), **asdict(snapshot),
                  "visible_warning": str(self.query_one("#warning", Static).render())}
        self.samples.append(sample)
        cpu_warning = any("CPU pressure" in warning for warning in snapshot.warnings)
        during_workload = (self.pressure_start is not None and sample["monotonic"] >= self.pressure_start
                           and (self.pressure_end is None or sample["monotonic"] <= self.pressure_end))
        high = valid_cpu(sample["cpu_percent"]) and sample["cpu_percent"] >= 90
        self._high_cpu_samples = self._high_cpu_samples + 1 if during_workload and high else 0
        visible = "CPU pressure" in sample["visible_warning"]
        if self._high_cpu_samples >= 3 and cpu_warning and visible and not self.pressure_shown:
            self.call_after_refresh(self._capture_pressure_view, sample, False)
        elif (self.pressure_shown and not self.pressure_cleared and self.pressure_end is not None
              and sample["monotonic"] > self.pressure_end and valid_cpu(sample["cpu_percent"])
              and sample["cpu_percent"] < 90 and not cpu_warning and not visible):
            self.call_after_refresh(self._capture_pressure_view, sample, True)

    def _capture_pressure_view(self, sample, recovered):
        # A queued screenshot must correspond to the measured sample that caused
        # it, not to a subsequent frame or to a pre-workload warning.
        widget = self.query_one("#warning", Static)
        if (not self.samples or self.samples[-1] is not sample or not widget.display
                or not widget.visible or str(widget.render()) != sample["visible_warning"]):
            return
        filename = "recovered.svg" if recovered else "pressure.svg"
        path = self.evidence / filename
        self.save_screenshot(str(path))
        rendered = screenshot_text(path)
        verified = bool(rendered and "CPU" in rendered and ("CPU pressure" in rendered) != recovered)
        sample["screenshot"] = filename
        sample["screenshot_verified"] = verified
        if verified and recovered:
            self.pressure_cleared = True
            self.recovery_capture_monotonic = sample["monotonic"]
        elif verified:
            self.pressure_shown = True
            self.pressure_capture_monotonic = sample["monotonic"]
            print("Measured CPU pressure alert captured in the rendered neobag UI", flush=True)


def valid_cpu(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100


def screenshot_text(path):
    """Read only displayed SVG text, not CSS, metadata, or XML comments."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return ""
    text = " ".join("".join(element.itertext()) for element in root.iter()
                    if element.tag.rsplit("}", 1)[-1] == "text")
    return " ".join(text.split())


def evaluate_comparison(comparison: dict) -> dict:
    """Accept complete sensor payloads while reporting known ROS diagnostics.

    Metadata-only source topics need not be recreated by a replay. Diagnostic
    extras are allowed only for these exact ROS names; their source messages
    still must be preserved. An unexpected duplicate sensor payload is a failure.
    """
    allowed_system_topics = {
        "/rosout", "/parameter_events", "/rosbag2_player/events/read_split", "/events/read_split",
    }
    topics = comparison["topics"]
    no_missing = comparison["missing_count"] == 0 and all(
        topic["missing_count"] == 0 for topic in topics.values()
    )
    sensor_exact = all(
        topic["type_match"] and topic["exact_payload_match"]
        for name, topic in topics.items()
        if name not in allowed_system_topics and topic["source_count"] > 0
    )
    unexpected_extra_topics = {
        name: topic["extra_count"]
        for name, topic in sorted(topics.items())
        if name not in allowed_system_topics and topic["extra_count"] > 0
    }
    allowed_system_extra_count = sum(
        topic["extra_count"] for name, topic in topics.items()
        if name in allowed_system_topics
    )
    return {
        "accepted": no_missing and sensor_exact and not unexpected_extra_topics,
        "sensor_exact": sensor_exact,
        "no_missing": no_missing,
        "unexpected_extra_topics": unexpected_extra_topics,
        "allowed_system_extra_count": allowed_system_extra_count,
    }


def evaluate_pressure(samples: list[dict], start: float | None, end: float | None) -> dict:
    """Require measured sustained load, the rendered warning, and real recovery."""
    result = {"during_workload_cpu_alert": False, "post_workload_recovered": False}
    if start is None or end is None or not math.isfinite(start) or not math.isfinite(end) or end <= start:
        return result
    high_streak = 0
    previous_time = -math.inf
    for sample in samples:
        when = sample.get("monotonic")
        if not isinstance(when, (int, float)) or not math.isfinite(when) or when <= previous_time:
            high_streak = 0
            continue
        previous_time = when
        cpu = sample.get("cpu_percent")
        warning = any("CPU pressure" in warning for warning in sample.get("warnings", []))
        visible = sample.get("visible_warning")
        if start <= when <= end and valid_cpu(cpu) and cpu >= 90:
            high_streak += 1
            if high_streak >= 3 and warning and isinstance(visible, str) and "CPU pressure" in visible:
                result["during_workload_cpu_alert"] = True
        else:
            high_streak = 0
        if (result["during_workload_cpu_alert"] and when > end and valid_cpu(cpu) and cpu < 90
                and not warning and isinstance(visible, str) and visible.strip() and "CPU pressure" not in visible):
            result["post_workload_recovered"] = True
    return result


async def run(args):
    if os.name != "posix" or not 1 <= args.domain <= 232:
        raise ValueError("Use Linux and an unused ROS domain between 1 and 232")
    os.environ.update(ROS_DOMAIN_ID=str(args.domain), ROS_LOCALHOST_ONLY="1",
                      ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST", ROS_STATIC_PEERS="")
    source, output = args.source.resolve(), args.output.resolve()
    if source == output or source in output.parents:
        raise ValueError("Output must not be inside the source bag")
    output.mkdir(parents=True, exist_ok=False)
    info = yaml.safe_load((source / "metadata.yaml").read_text())["rosbag2_bagfile_information"]
    expected = {item["topic_metadata"]["name"]: item["message_count"] for item in info["topics_with_message_count"]}
    duration = info["duration"]["nanoseconds"] / 1e9
    graph = await asyncio.to_thread(TopicDiscovery().scan)
    foreign = [topic.name for topic in graph if topic.name not in ("/rosout", "/parameter_events")]
    if foreign:
        raise RuntimeError(f"Chosen domain has existing topics; choose another: {foreign}")
    nodes = await asyncio.to_thread(subprocess.run,
        ["ros2", "node", "list", "--no-daemon", "--spin-time", "1"],
        capture_output=True, text=True, timeout=15, check=True)
    if any(line.startswith("/") for line in nodes.stdout.splitlines()):
        raise RuntimeError("Chosen domain contains existing ROS nodes; choose another")
    qos = None
    qos_sha256 = None
    if args.qos_overrides:
        data = args.qos_overrides.read_bytes()
        qos = output / "qos-overrides.yaml"
        qos.write_bytes(data)
        qos_sha256 = hashlib.sha256(data).hexdigest()
    recorder = CaptureRecorder(qos_overrides=qos)
    app = CaptureApp(output, output_parent=output, bag_name="recorded", recorder=recorder)
    player_log = (output / "player.log").open("w")
    command = ["ros2", "bag", "play", "--input", str(source), "--start-paused",
               "--disable-keyboard-controls", "--wait-for-all-acked", "10000",
               "--rate", str(args.rate), "--read-ahead-queue-size", "1000"]
    playback_qos_sha256 = None
    if args.playback_qos_overrides:
        data = args.playback_qos_overrides.read_bytes()
        playback_qos = output / "playback-qos-overrides.yaml"
        playback_qos.write_bytes(data)
        playback_qos_sha256 = hashlib.sha256(data).hexdigest()
        command.extend(("--qos-profile-overrides-path", str(playback_qos)))
    player = subprocess.Popen(command, stdout=player_log, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, start_new_session=True)
    loads = []
    resume_at = None
    pressure_at = None
    pressure_end = None
    started = time.monotonic()
    result = {"source_name": source.name, "domain": args.domain, "rate": args.rate,
              "qos_overrides_sha256": qos_sha256,
              "playback_qos_overrides_sha256": playback_qos_sha256,
              "pressure_requested": args.cpu_pressure, "source_message_count": info["message_count"],
              "source_topic_count": len(expected), "source_duration_seconds": duration}
    try:
        async with app.run_test(size=(140, 42)) as pilot:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if player.poll() is not None:
                    raise RuntimeError("Player exited before capture; inspect player.log")
                found = {topic.name for topic in app.topics}
                if all(name in found for name, count in expected.items() if count):
                    break
                await pilot.pause(0.25)
            else:
                raise RuntimeError(f"Source topics not discovered: {set(expected) - found}")
            # Exercise the real All button and preserve exactly the current graph.
            assert await pilot.click("#all")
            await pilot.pause(0.3)
            result["selected_topics"] = sorted(app.selected)
            app.save_screenshot(str(output / "ready.svg"))
            assert await pilot.click("#start")
            deadline = time.monotonic() + 45
            subscribed = set()
            while time.monotonic() < deadline:
                text = "\n".join(recorder.captured_logs)
                subscribed = set(re.findall(r"Subscribed to topic ['\"]([^'\"]+)['\"]", text))
                if all(name in subscribed for name, count in expected.items() if count):
                    break
                if recorder.state == "failed":
                    raise RuntimeError(recorder.error)
                await pilot.pause(0.2)
            else:
                raise RuntimeError(f"Recorder subscriptions not ready: {set(n for n,c in expected.items() if c) - subscribed}")
            result["subscriptions_created_before_resume"] = sorted(subscribed)
            await pilot.pause(2)
            paused = await asyncio.to_thread(subprocess.run,
                ["ros2", "service", "call", "/rosbag2_player/is_paused", "rosbag2_interfaces/srv/IsPaused", "{}"],
                capture_output=True, text=True, timeout=20, check=True)
            if "paused=True" not in paused.stdout:
                raise RuntimeError(f"Player is not paused before resume: {paused.stdout}")
            result["paused_verified_before_resume"] = True
            response = await asyncio.to_thread(subprocess.run,
                ["ros2", "service", "call", "/rosbag2_player/resume", "rosbag2_interfaces/srv/Resume", "{}"],
                capture_output=True, text=True, timeout=20, check=True)
            result["resume_response"] = response.stdout
            resume_at = time.monotonic()
            print(f"Playback resumed: {len(subscribed)} subscriptions ready, {duration:.2f}s at {args.rate}x", flush=True)
            next_progress = resume_at + 25
            while player.poll() is None:
                elapsed = time.monotonic() - resume_at
                if args.cpu_pressure and pressure_at is None and elapsed >= 15:
                    pressure_at = time.monotonic()
                    app.pressure_start = pressure_at
                    worker = "import os,time; os.nice(10); end=time.monotonic()+25; x=1\nwhile time.monotonic()<end:\n for _ in range(10000): x=(x*1664525+1013904223)&0xffffffff"
                    for _ in range(os.cpu_count() or 1):
                        loads.append(subprocess.Popen([sys.executable, "-c", worker], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
                    print(f"Started {len(loads)} bounded CPU workers for 25 seconds", flush=True)
                if loads and pressure_end is None and all(load.poll() is not None for load in loads):
                    pressure_end = time.monotonic()
                    app.pressure_end = pressure_end
                    print("CPU workers finished; checking alert recovery", flush=True)
                if time.monotonic() >= next_progress:
                    print(f"Replay elapsed {elapsed:.0f}s; recorder={recorder.state}", flush=True)
                    next_progress += 25
                if elapsed > duration / args.rate + 120:
                    raise RuntimeError("Playback exceeded bounded acceptance timeout")
                if not recorder.is_active:
                    raise RuntimeError(recorder.error or "Recorder exited during playback")
                await pilot.pause(0.25)
            result["player_returncode"] = player.returncode
            result["playback_wall_seconds"] = time.monotonic() - resume_at
            if player.returncode != 0:
                raise RuntimeError(f"Player failed: {player.returncode}")
            await pilot.pause(5)
            app.save_screenshot(str(output / "recording.svg"))
            assert await pilot.click("#stop")
            deadline = time.monotonic() + 60
            while recorder.is_active and time.monotonic() < deadline:
                await pilot.pause(0.2)
            recorder.poll()
            if recorder.state != "finished":
                raise RuntimeError(recorder.error or recorder.state)
            app.save_screenshot(str(output / "saved.svg"))
            await pilot.press("ctrl+q")
        result.update(recorder_state=recorder.state, recorder_returncode=recorder.returncode,
                      cpu_pressure_alert_visible=app.pressure_shown,
                      cpu_pressure_alert_cleared=app.pressure_cleared,
                      pressure_screenshot_monotonic=app.pressure_capture_monotonic,
                      recovery_screenshot_monotonic=app.recovery_capture_monotonic,
                      ui_evidence_mode="Textual headless rendering with real ROS and host telemetry",
                      pressure_worker_count=len(loads), resource_samples=len(app.samples),
                      pressure_start_monotonic=pressure_at, pressure_end_monotonic=pressure_end)
    finally:
        cleanup_errors = []
        try:
            try:
                if player.poll() is None:
                    os.killpg(player.pid, signal.SIGINT)
                    try:
                        await asyncio.to_thread(player.wait, timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(player.pid, signal.SIGTERM)
                        await asyncio.to_thread(player.wait, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                cleanup_errors.append(f"player: {exc}")
            for load in loads:
                try:
                    if load.poll() is None:
                        load.terminate()
                    await asyncio.to_thread(load.wait, timeout=10)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    cleanup_errors.append(f"CPU worker: {exc}")
        finally:
            try:
                await asyncio.to_thread(app.finish_cleanup)
            finally:
                player_log.close()
                (output / "recorder.log").write_text("\n".join(recorder.captured_logs + recorder.drain_logs()), encoding="utf-8")
                (output / "resources.json").write_text(json.dumps(app.samples, indent=2), encoding="utf-8")
                result["elapsed_seconds"] = time.monotonic() - started
                result["cleanup_errors"] = cleanup_errors
                (output / "run.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("Recording finalized; comparing every serialized payload…", flush=True)
    comparison = await asyncio.to_thread(compare_bags, source, output / "recorded")
    (output / "comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    print(json.dumps({key: comparison[key] for key in
                     ("exact_payload_match", "source_count", "destination_count", "missing_count", "extra_count")}), flush=True)
    acceptance = evaluate_comparison(comparison)
    if args.cpu_pressure:
        acceptance["pressure"] = evaluate_pressure(app.samples, pressure_at, pressure_end)
        acceptance["pressure"]["pressure_screenshot_verified"] = (
            app.pressure_shown and "CPU pressure" in screenshot_text(output / "pressure.svg"))
        recovered_text = screenshot_text(output / "recovered.svg")
        acceptance["pressure"]["recovery_screenshot_verified"] = (
            app.pressure_cleared and bool(recovered_text) and "CPU pressure" not in recovered_text)
        acceptance["accepted"] = acceptance["accepted"] and all(acceptance["pressure"].values())
    acceptance["accepted"] = acceptance["accepted"] and not result["cleanup_errors"]
    (output / "acceptance.json").write_text(json.dumps(acceptance, indent=2), encoding="utf-8")
    print(json.dumps(acceptance), flush=True)
    if not acceptance["accepted"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--domain", type=int, required=True)
    parser.add_argument("--rate", type=float, default=1.0)
    parser.add_argument("--cpu-pressure", action="store_true")
    parser.add_argument("--qos-overrides", type=Path, help="Verified per-topic recorder QoS YAML")
    parser.add_argument("--playback-qos-overrides", type=Path, help="Optional separate player QoS; retained with the evidence")
    args = parser.parse_args()
    if args.rate <= 0:
        parser.error("--rate must be positive")
    asyncio.run(run(args))

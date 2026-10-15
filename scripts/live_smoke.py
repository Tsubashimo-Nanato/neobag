"""Opt-in Linux/ROS integration check in an isolated DDS domain.

Run explicitly: ROS_DOMAIN_ID=217 python scripts/live_smoke.py
Publishes only /neobag_test/text; never starts vehicle nodes or replays old bags.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time

from neobag.app import NeoBagApp
from neobag.recorder import Recorder
from neobag.resources import ResourceMonitor
from neobag.topics import TopicDiscovery
from textual.widgets import Input


async def main() -> None:
    if not os.environ.get("ROS_DOMAIN_ID") or os.environ["ROS_DOMAIN_ID"] == "0":
        raise SystemExit("Set an unused nonzero ROS_DOMAIN_ID before this integration check.")
    evidence = Path("evidence")
    evidence.mkdir(exist_ok=True)
    name = datetime.now().strftime("smoke_%Y%m%d_%H%M%S")
    parent = evidence / "bags"
    recorder = Recorder()
    with (evidence / f"{name}_publisher.log").open("w") as output:
        publisher = subprocess.Popen(
            ["ros2", "topic", "pub", "/neobag_test/text", "std_msgs/msg/String", "{data: neobag_smoke}", "--rate", "20"],
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            for _ in range(8):
                topics = await asyncio.to_thread(TopicDiscovery().scan)
                if any(t.name == "/neobag_test/text" for t in topics):
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Test publisher was not discovered")
            app = NeoBagApp(output_parent=parent.resolve(), bag_name=name, recorder=recorder)
            async with app.run_test(size=(120, 36)) as pilot:
                for _ in range(80):
                    if any(t.name == "/neobag_test/text" for t in app.topics):
                        break
                    await pilot.pause(0.1)
                assert any(t.name == "/neobag_test/text" for t in app.topics)
                app.selected = {"/neobag_test/text"}
                app.rebuild_topics()
                await pilot.pause()
                app.save_screenshot(str(evidence / f"{name}_ready.svg"))
                await pilot.click("#start")
                for _ in range(80):
                    if recorder.is_active:
                        break
                    await pilot.pause(0.1)
                assert recorder.is_active, app.notice
                await pilot.pause(5)
                app.save_screenshot(str(evidence / f"{name}_recording.svg"))
                await pilot.click("#stop")
                for _ in range(150):
                    if recorder.state in ("finished", "failed") and not recorder.is_active:
                        break
                    await pilot.pause(0.1)
                assert recorder.state == "finished", (recorder.state, recorder.error, list(app.lines))
                app.save_screenshot(str(evidence / f"{name}_saved.svg"))
                await pilot.press("ctrl+q")
            bag = parent / name
            db = next(bag.glob("*.db3"))
            with sqlite3.connect(str(db)) as con:
                rows = con.execute("SELECT topics.name, COUNT(*), MIN(timestamp), MAX(timestamp) FROM messages JOIN topics ON topics.id=messages.topic_id GROUP BY topics.name").fetchall()
            assert rows and rows[0][0] == "/neobag_test/text" and rows[0][1] >= 20, rows
            info = subprocess.run(["ros2", "bag", "info", str(bag)], capture_output=True, text=True, timeout=15, check=True).stdout
            monitor = ResourceMonitor()
            monitor.sample(parent)
            await asyncio.sleep(1)
            result = {"domain": os.environ["ROS_DOMAIN_ID"], "bag": str(bag.resolve()), "messages": rows,
                      "resources": asdict(monitor.sample(parent)), "bag_info": info,
                      "recorder_state": recorder.state, "recorder_returncode": recorder.returncode,
                      "ui": "Textual Pilot with real discovery, recording and stop, 120x36"}
            (evidence / "live_smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result, indent=2))
        finally:
            if recorder.is_active:
                recorder.request_stop()
                for _ in range(200):
                    recorder.poll()
                    if not recorder.is_active:
                        break
                    await asyncio.sleep(0.1)
            if publisher.poll() is None:
                os.killpg(publisher.pid, signal.SIGINT)
                try:
                    publisher.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(publisher.pid, signal.SIGTERM)
                    publisher.wait(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())

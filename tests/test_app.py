"""Headless terminal interaction tests with explicit fake ROS/recorder data.

Set NEOBAG_CAPTURE_UI=1 to save simulated UI SVG evidence at both terminal sizes.
These tests exercise Textual events/layout, not a ROS host or GPU driver.
"""

from __future__ import annotations

import os
from pathlib import Path
import asyncio
import threading
import time

import pytest
from textual.widgets import Button, Input, Select, Static

from neobag.app import NeoBagApp
from neobag.resources import ResourceSnapshot
from neobag.topics import Topic
from neobag.profiles import ProfileStore
from neobag.topic_tree import TopicTree, SelectedTopics


@pytest.fixture(autouse=True)
def isolated_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


SAMPLE_TOPICS = [
    Topic("/front/image_raw", ("sensor_msgs/msg/Image",), "Camera"),
    Topic("/rear/image_raw", ("sensor_msgs/msg/Image",), "Camera"),
    Topic("/points", ("sensor_msgs/msg/PointCloud2",), "Point clouds"),
    Topic("/imu", ("sensor_msgs/msg/Imu",), "IMU"),
    Topic("/odom", ("nav_msgs/msg/Odometry",), "Odometry"),
    Topic("/cmd_vel", ("geometry_msgs/msg/Twist",), "Control"),
    Topic("/rosout", ("rcl_interfaces/msg/Log",), "Diagnostics"),
]
ALL_NAMES = {topic.name for topic in SAMPLE_TOPICS}
CAMERA_NAMES = {"/front/image_raw", "/rear/image_raw"}


class FakeDiscovery:
    def __init__(self):
        self.topics = list(SAMPLE_TOPICS)
        self.error: Exception | None = None
        self.calls = 0

    def scan(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.topics)


class FakeRecorder:
    def __init__(self):
        self.state = "idle"
        self.started_at = None
        self.error = None
        self.output_path = None
        self.start_calls = []
        self.stop_calls = 0
        self.logs = []
        self.start_error = None

    @property
    def is_active(self):
        return self.state in {"recording", "stopping"}

    def start(self, topics, parent, name, storage):
        self.start_calls.append((list(topics), parent, name, storage))
        if self.start_error:
            raise self.start_error
        self.output_path = parent / name
        self.started_at = time.monotonic()
        self.state = "recording"
        return self.output_path

    def request_stop(self):
        self.stop_calls += 1
        self.state = "stopping"

    def finish_stop(self):
        self.state = "finished"

    def poll(self):
        return self.state

    def drain_logs(self):
        result, self.logs = self.logs, []
        return result


class FakeMonitor:
    def __init__(self):
        self.snapshot = ResourceSnapshot(
            cpu_percent=21.0, ram_percent=34.0,
            ram_used_bytes=5 * 2**30, ram_total_bytes=16 * 2**30,
            gpu_percent=12.0, gpu_source="Simulated test GPU",
            disk_free_bytes=128 * 2**30, disk_percent=37.0,
            warnings=(),
        )
        self.calls = []

    def sample(self, parent):
        self.calls.append(parent)
        return self.snapshot


@pytest.fixture
def bundle(tmp_path):
    discovery, recorder, monitor = FakeDiscovery(), FakeRecorder(), FakeMonitor()
    app = NeoBagApp(
        output_parent=tmp_path / "bags", bag_name="sample_run", storage="sqlite3",
        discovery=discovery, recorder=recorder, monitor=monitor,
    )
    return app, discovery, recorder, monitor


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def click_button(app, pilot, selector):
    """Click inside either a regular bordered or a compact one-line button."""
    button = app.screen.query_one(selector, Button)
    clicked = await pilot.click(selector, offset=(2, button.region.height // 2))
    # The click queues Button.Pressed on the app after the widget handles it.
    await pilot.pause()
    return clicked


async def choose_topic(app, pilot, name, *, mouse=True):
    tree = app.query_one("#topics", TopicTree)
    tree.root.expand_all()
    await pilot.pause()
    node = tree.nodes["topic:" + name]
    tree.move_cursor(node)
    tree.scroll_to_node(node, animate=False)
    tree.focus()
    await pilot.pause()
    if mouse:
        depth = 0
        parent = node.parent
        while parent is not tree.root:
            depth += 1
            parent = parent.parent
        x = tree.guide_depth * depth + 3
        y = node.line - int(tree.scroll_y) + 1
        assert await pilot.click("#topics", offset=(x, y))
    else:
        await pilot.press("space")
    await pilot.pause()


def capture(app, name):
    if os.environ.get("NEOBAG_CAPTURE_UI") == "1":
        destination = Path(__file__).resolve().parents[1] / "evidence" / "ui"
        destination.mkdir(parents=True, exist_ok=True)
        app.save_screenshot(filename=f"simulated-{name}.svg", path=str(destination))


def assert_in_view(app, widget_id):
    widget = app.query_one(widget_id)
    assert widget.visible, f"{widget_id} must be visible"
    region = widget.region
    assert region.width > 0 and region.height > 0, f"{widget_id} has no clickable area"
    assert region.x >= 0 and region.y >= 0, f"{widget_id} is above/left of viewport: {region}"
    assert region.right <= app.size.width, f"{widget_id} extends past the right edge: {region}"
    assert region.bottom <= app.size.height, f"{widget_id} extends below the viewport: {region}"


@pytest.mark.parametrize("size", [(120, 36), (80, 24)])
async def test_controls_are_visible_clickable_and_recording_layout_fits(bundle, size):
    app, _, recorder, _ = bundle
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        for selector in (
            "#cpu", "#gpu", "#ram", "#disk", "#search", "#category",
            "#all", "#none", "#expand", "#collapse", "#topics", "#selected-topics",
            "#parent", "#name", "#start", "#stop", "#refresh", "#summary",
        ):
            assert_in_view(app, selector)
        if size == (80, 24):
            assert app.query_one("#topics").region.height >= 5
            assert app.query_one("#selected-topics").region.height >= 4
        assert await click_button(app, pilot, "#all")
        assert app.selected == ALL_NAMES
        assert await pilot.click("#parent", offset=(2, 1))
        assert app.query_one("#parent", Input).has_focus
        assert await pilot.click("#name", offset=(2, 1))
        assert app.query_one("#name", Input).has_focus
        capture(app, f"{size[0]}x{size[1]}-ready")
        assert await click_button(app, pilot, "#start")
        await settle(app, pilot)
        assert recorder.state == "recording"
        app.poll_recorder()
        await pilot.pause()
        assert_in_view(app, "#stop")
        assert not app.query_one("#stop", Button).disabled
        capture(app, f"{size[0]}x{size[1]}-recording")
        assert await click_button(app, pilot, "#stop")
        assert recorder.state == "stopping"
        recorder.finish_stop()
        app.poll_recorder()


async def test_select_all_and_none_only_change_current_candidates(bundle):
    app, _, _, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        app.query_one("#category", Select).value = "Camera"
        app.query_one("#search", Input).value = "front"
        await pilot.pause()
        assert [topic.name for topic in app.visible_topics()] == ["/front/image_raw"]
        assert await click_button(app, pilot, "#all")
        assert app.selected == {"/front/image_raw"}
        app.query_one("#search", Input).value = "rear"
        await pilot.pause()
        assert app.selected == {"/front/image_raw"}
        await pilot.pause(0.3)
        assert await click_button(app, pilot, "#all")
        assert app.selected == CAMERA_NAMES
        assert {option.id for option in app.query_one("#selected-topics", SelectedTopics).options} == CAMERA_NAMES
        assert await click_button(app, pilot, "#none")
        assert app.selected == {"/front/image_raw"}
        app.query_one("#search", Input).value = "no matches"
        await pilot.pause()
        assert app.selected == {"/front/image_raw"}
        app.query_one("#search", Input).value = ""
        app.query_one("#category", Select).value = "all"
        await pilot.pause(0.3)
        assert await click_button(app, pilot, "#none")
        assert app.selected == set()


async def test_search_and_category_selection_preserve_other_candidates(bundle):
    app, _, _, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        app.query_one("#category", Select).value = "Camera"
        app.query_one("#search", Input).value = "front"
        await pilot.pause()
        assert await click_button(app, pilot, "#all")
        assert app.selected == {"/front/image_raw"}
        app.query_one("#category", Select).value = "IMU"
        app.query_one("#search", Input).value = ""
        # Textual ignores a second click while the same Button is still active.
        await pilot.pause(0.3)
        assert await click_button(app, pilot, "#all")
        assert app.selected == {"/front/image_raw", "/imu"}
        app.query_one("#category", Select).value = "Camera"
        await pilot.pause()
        assert await click_button(app, pilot, "#none")
        assert app.selected == {"/imu"}


async def test_individual_mouse_and_space_selection_survive_filter_and_refresh(bundle):
    app, discovery, _, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await choose_topic(app, pilot, "/front/image_raw")
        assert app.selected == {"/front/image_raw"}
        await choose_topic(app, pilot, "/rear/image_raw", mouse=False)
        assert app.selected == CAMERA_NAMES
        app.query_one("#search", Input).value = "imu"
        await pilot.pause()
        assert app.selected == CAMERA_NAMES
        assert not app.query_one("#topics", TopicTree).chosen & {t.name for t in app.visible_topics()}
        assert {option.id for option in app.query_one("#selected-topics", SelectedTopics).options} == CAMERA_NAMES
        app.query_one("#search", Input).value = ""
        await pilot.pause()
        assert app.query_one("#topics", TopicTree).chosen == CAMERA_NAMES
        discovery.topics = [topic for topic in SAMPLE_TOPICS if topic.name != "/rear/image_raw"]
        discovery.topics.append(Topic("/new", ("std_msgs/msg/String",), "Other"))
        assert await click_button(app, pilot, "#refresh")
        await settle(app, pilot)
        assert app.selected == CAMERA_NAMES
        assert "/new" not in app.selected
        assert next(topic for topic in app.visible_topics() if topic.name == "/rear/image_raw").category == "Offline"
        assert "1 offline" in str(app.query_one("#summary", Static).content)


async def test_topics_only_rescan_on_manual_refresh_while_resources_keep_updating(bundle):
    app, discovery, _, monitor = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        assert discovery.calls == 1
        await choose_topic(app, pilot, "/front/image_raw")
        selection = set(app.selected)
        samples_before = len(monitor.calls)
        discovery.topics.append(Topic("/new", ("std_msgs/msg/String",), "Other"))

        # Cross the former five-second discovery interval. Only the separate
        # one-second resource timer should continue doing background work.
        await pilot.pause(5.2)
        await settle(app, pilot)
        assert discovery.calls == 1
        assert "/new" not in {topic.name for topic in app.topics}
        assert len(monitor.calls) > samples_before
        assert app.selected == selection

        assert await click_button(app, pilot, "#refresh")
        await settle(app, pilot)
        assert discovery.calls == 2
        assert "/new" in {topic.name for topic in app.topics}
        assert app.selected == selection

        discovery.topics.append(Topic("/another", ("std_msgs/msg/String",), "Other"))
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert discovery.calls == 3
        assert "/another" in {topic.name for topic in app.topics}
        assert app.selected == selection


async def test_start_uses_exact_selection_destination_name_and_storage_then_unlocks_on_finish(bundle, tmp_path):
    app, discovery, recorder, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await choose_topic(app, pilot, "/front/image_raw")
        destination = tmp_path / "custom bags"
        app.query_one("#parent", Input).value = str(destination)
        app.query_one("#name", Input).value = "  lap_2026_10_15  "
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        assert recorder.start_calls == [(["/front/image_raw"], destination, "lap_2026_10_15", "sqlite3")]
        for selector in ("#all", "#none", "#parent", "#name", "#start", "#refresh"):
            assert app.query_one(selector).disabled
        scans_before = discovery.calls
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert discovery.calls == scans_before
        await pilot.press("ctrl+s")
        assert len(recorder.start_calls) == 1
        await pilot.press("ctrl+t")
        assert recorder.stop_calls == 1
        assert recorder.state == "stopping"
        assert "waiting for rosbag to flush" in str(app.query_one("#summary", Static).content)
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert discovery.calls == scans_before
        recorder.finish_stop()
        app.poll_recorder()
        await pilot.pause()
        assert not app.query_one("#topics").disabled
        assert not app.query_one("#start").disabled
        assert app.query_one("#stop").disabled
        assert "Saved" in str(app.query_one("#summary", Static).content)
        assert app.query_one("#name", Input).value != "lap_2026_10_15"


async def test_quit_waits_until_recorder_has_finalized(bundle):
    app, _, recorder, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        assert await click_button(app, pilot, "#all")
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        await pilot.press("ctrl+q")
        assert recorder.stop_calls == 1
        assert recorder.state == "stopping"
        assert app.is_running
        recorder.finish_stop()
        app.poll_recorder()
        await pilot.pause()
        assert not app.is_running


async def test_discovery_failure_blocks_recording_until_successful_refresh(bundle):
    app, discovery, recorder, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)

        async def refresh_once():
            button = app.query_one("#refresh", Button)

            async def released():
                # Textual ignores mouse clicks until the prior active effect
                # ends. Waiting for workers alone does not wait for that timer.
                while button.has_class("-active"):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(released(), timeout=3)
            previous_calls = discovery.calls
            assert await click_button(app, pilot, "#refresh")

            async def scan_completed():
                # The click may have queued its worker after settle() took its
                # worker snapshot; wait for this exact scan and its UI result.
                while discovery.calls <= previous_calls or app._scanning:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(scan_completed(), timeout=3)
            await settle(app, pilot)
            assert discovery.calls == previous_calls + 1

        assert await click_button(app, pilot, "#all")
        discovery.error = RuntimeError("ROS 2 discovery unavailable; source setup.bash")
        await refresh_once()
        assert app.query_one("#start", Button).disabled
        assert "source setup.bash" in str(app.query_one("#summary", Static).content)
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        assert recorder.start_calls == []
        discovery.error = None
        await refresh_once()
        assert not app.query_one("#start", Button).disabled


async def test_start_failure_releases_controls_and_keeps_actionable_error(bundle):
    app, _, recorder, _ = bundle
    recorder.start_error = FileExistsError("Bag output already exists")
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        assert await click_button(app, pilot, "#all")
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        assert not app.query_one("#start").disabled
        assert not app.query_one("#name").disabled
        assert app.query_one("#stop").disabled
        assert "Bag output already exists" in str(app.query_one("#summary", Static).content)


@pytest.mark.parametrize("size", [(120, 36), (80, 24)])
async def test_resource_pressure_and_unavailable_gpu_render_without_claiming_data_loss(bundle, size):
    app, _, _, monitor = bundle
    monitor.snapshot = ResourceSnapshot(
        cpu_percent=96.0, ram_percent=93.0,
        ram_used_bytes=15 * 2**30, ram_total_bytes=16 * 2**30,
        gpu_percent=None, gpu_source="N/A (unsupported test GPU)",
        disk_free_bytes=2 * 2**30, disk_percent=98.0,
        warnings=("Sustained CPU pressure (96%): recording rate may fall or messages may be dropped.",),
    )
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        assert str(app.query_one("#cpu", Static).content) == "CPU  96%"
        assert str(app.query_one("#gpu", Static).content) == "GPU  N/A"
        assert str(app.query_one("#ram", Static).content) == "RAM  93%"
        warning = app.query_one("#warning", Static)
        assert warning.has_class("pressure")
        assert "may fall" in str(warning.content)
        assert "dropped" in str(warning.content)
        capture(app, f"{size[0]}x{size[1]}-pressure")


async def test_refresh_preserves_highlighted_topic_and_scroll_position_in_long_list(bundle):
    app, discovery, _, _ = bundle
    discovery.topics = [
        Topic(f"/camera_{number:03d}/image_raw", ("sensor_msgs/msg/Image",), "Camera")
        for number in range(60)
    ]
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        widget = app.query_one("#topics", TopicTree)
        widget.root.expand_all()
        await pilot.pause()
        widget.focus()
        widget.move_cursor(widget.nodes["topic:/camera_035/image_raw"])
        widget.scroll_to_node(widget.cursor_node, animate=False)
        await pilot.pause()
        before_name = widget.cursor_node.data.key
        before_scroll = widget.scroll_y
        assert before_scroll > 0
        scans_before = discovery.calls
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert discovery.calls > scans_before
        assert widget.cursor_node.data.key == before_name
        assert widget.scroll_y == before_scroll
        assert widget.has_focus
        # Retain the topic identity even when a new earlier row moves its index.
        discovery.topics.insert(0, Topic("/added/image_raw", ("sensor_msgs/msg/Image",), "Camera"))
        await app.refresh_topics()
        await pilot.pause()
        assert widget.cursor_node.data.key == before_name
        assert widget.scroll_y == before_scroll


async def test_forced_app_exit_requests_owned_recorder_stop(bundle):
    app, _, recorder, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        assert await click_button(app, pilot, "#all")
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        assert recorder.state == "recording"
        app.exit()
    assert recorder.stop_calls == 1
    assert recorder.state == "stopping"
    recorder.finish_stop()


async def test_forced_exit_joins_pending_start_then_requests_stop(tmp_path):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    transitions = []

    class SlowRecorder(FakeRecorder):
        def start(self, topics, parent, name, storage):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("Test recorder release did not arrive")
            result = super().start(topics, parent, name, storage)
            transitions.append("started")
            finished.set()
            return result

        def request_stop(self):
            assert finished.is_set(), "Stop must follow completion of the in-flight start"
            transitions.append("stop")
            super().request_stop()

    recorder = SlowRecorder()
    app = NeoBagApp(output_parent=tmp_path, bag_name="slow_start", discovery=FakeDiscovery(),
                    recorder=recorder, monitor=FakeMonitor())
    release_timer = None
    try:
        async with app.run_test(size=(120, 36)) as pilot:
            await settle(app, pilot)
            assert await click_button(app, pilot, "#all")
            await pilot.press("ctrl+s")
            assert await asyncio.to_thread(entered.wait, 1)
            assert app._starting
            assert not recorder.is_active
            # A separate OS timer can release the start even while unmount joins
            # the dedicated executor synchronously during event-loop teardown.
            release_timer = threading.Timer(0.25, release.set)
            release_timer.start()
            app.exit()
        assert finished.is_set()
        assert transitions == ["started", "stop"]
        assert recorder.state == "stopping"
    finally:
        release.set()
        if release_timer is not None:
            release_timer.cancel()
            release_timer.join(timeout=1)
        recorder.finish_stop()


def test_finish_cleanup_waits_for_finalization_and_reports_saved_path(bundle, monkeypatch, capsys):
    app, _, recorder, _ = bundle
    recorder.start(["/imu"], Path("bags"), "cleanup_test", "sqlite3")
    poll_count = 0

    def eventual_finish():
        nonlocal poll_count
        poll_count += 1
        if poll_count < 3:
            recorder.error = "Test recorder is still flushing"
        else:
            recorder.error = None
            recorder.finish_stop()
        return recorder.state

    recorder.poll = eventual_finish
    monkeypatch.setattr("neobag.app.time.sleep", lambda _: None)
    app.finish_cleanup()
    output = capsys.readouterr().out
    assert recorder.stop_calls == 1
    assert poll_count >= 3
    assert recorder.state == "finished"
    assert output.count("Test recorder is still flushing") == 1
    assert "waiting for rosbag to finish saving" in output
    assert f"saved {recorder.output_path}" in output


async def test_logs_modal_remains_usable_while_recorder_and_resource_timers_tick(bundle):
    app, _, recorder, monitor = bundle
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert await click_button(app, pilot, "#all")
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        recorder.logs.append("Simulated recorder started")
        app.poll_recorder()
        samples_before = len(monitor.calls)
        await pilot.press("ctrl+l")
        assert app.screen.query_one("#log").visible
        # Allow both the 250 ms recorder poll and 1 s telemetry timers to run.
        await pilot.pause(1.1)
        assert app.is_running
        assert len(monitor.calls) > samples_before
        assert app.screen.query_one("#close-log").visible
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#stop").visible
        assert await click_button(app, pilot, "#stop")
        recorder.finish_stop()
        app.poll_recorder()


async def test_late_timer_callbacks_after_ui_unmount_are_ignored(bundle):
    app, discovery, _, monitor = bundle
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
    assert not app.is_running
    scans, samples = discovery.calls, len(monitor.calls)
    # A queued callback may arrive after Textual has removed the widgets.
    app.poll_recorder()
    await app.refresh_topics()
    await app.sample_resources()
    app.show_resources(monitor.snapshot)
    app.lock_controls(False)
    app.update_summary()
    assert discovery.calls == scans
    assert len(monitor.calls) == samples


@pytest.mark.parametrize("size", [(80, 24), (120, 36)])
async def test_named_profiles_save_load_edit_and_restart(bundle, tmp_path, size):
    app, _, _, _ = bundle
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.selected = {"/imu", "/offline/camera"}
        app.rebuild_topics()
        assert await click_button(app, pilot, "#profiles")
        await pilot.pause()
        app.screen.query_one("#profile-name", Input).value = "Sensors"
        assert await click_button(app, pilot, "#profile-save")
        await pilot.pause()
        assert app.profiles.read()["profiles"]["Sensors"]["topics"] == ["/imu", "/offline/camera"]
        assert await click_button(app, pilot, "#profile-back")
        await pilot.pause()
        assert await click_button(app, pilot, "#none")
        assert not app.selected
        await pilot.press("ctrl+o")
        await pilot.pause()
        app.screen.query_one("#profile-choice", Select).value = "Sensors"
        assert await click_button(app, pilot, "#profile-load")
        await pilot.pause()
        assert app.selected == {"/imu", "/offline/camera"}
        app.selected.add("/odom")
        app.rebuild_topics()
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert await click_button(app, pilot, "#profile-save")
        await pilot.pause()
        await pilot.pause(0.4)
        app.screen.query_one("#profile-name", Input).value = "Copy"
        assert await click_button(app, pilot, "#profile-save")
        await pilot.pause()
        assert set(app.profiles.read()["profiles"]) >= {"Sensors", "Copy"}
        if os.environ.get("NEOBAG_CAPTURE_UI"):
            path = Path("evidence/profiles")
            path.mkdir(parents=True, exist_ok=True)
            app.save_screenshot(str(path / f"profiles-{size[0]}x{size[1]}.svg"))
    restarted = NeoBagApp(discovery=FakeDiscovery(), recorder=FakeRecorder(), monitor=FakeMonitor())
    assert restarted.profile_name == "Copy"
    assert restarted.selected == {"/imu", "/offline/camera", "/odom"}
    restarted.finish_cleanup()


async def test_reported_loss_remains_visible_after_resources_recover(bundle):
    app, _, recorder, monitor = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        recorder.transport_lost = 38
        recorder.state = "finished"
        recorder.output_path = Path("saved-bag")
        app.poll_recorder()
        app.show_resources(monitor.snapshot)
        assert "LOST" in str(app.query_one("#record-state", Static).content)
        assert "incomplete" in str(app.query_one("#warning", Static).content)
        assert "38 messages lost" in str(app.query_one("#summary", Static).content)


async def test_offline_profile_topics_can_be_removed_individually(bundle):
    app, _, _, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        app.selected = {"/imu", "/offline/camera"}
        app.rebuild_topics()
        app.query_one("#category", Select).value = "Offline"
        await pilot.pause()
        assert {t.name for t in app.visible_topics()} == {"/offline/camera"}
        await choose_topic(app, pilot, "/offline/camera")
        assert app.selected == {"/imu"}

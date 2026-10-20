"""Mouse/keyboard selection invariants for the namespace tree and selection tray."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from textual.widgets import Input

from neobag.topic_tree import PALETTE, TYPE_COLORS, TopicTree, SelectedTopics, type_color
from neobag.topics import Topic
from test_app import bundle, choose_topic, click_button, isolated_profiles, settle


SENSORS = [
    Topic("/aiformula_sensing/zed_node/left_raw/image_raw_color", ("sensor_msgs/msg/Image",), "Camera"),
    Topic("/aiformula_sensing/zed_node/left_raw/camera_info", ("sensor_msgs/msg/CameraInfo",), "Camera"),
    Topic("/aiformula_sensing/zed_node/right_raw/image_raw_color", ("sensor_msgs/msg/Image",), "Camera"),
    Topic("/aiformula_sensing/vectornav/imu", ("sensor_msgs/msg/Imu",), "IMU"),
    Topic("/aiformula_sensing/vectornav/temperature", ("sensor_msgs/msg/Temperature",), "IMU"),
    Topic("/aiformula_control/game_pad/cmd_vel", ("geometry_msgs/msg/Twist",), "Control"),
    Topic("/tf", ("tf2_msgs/msg/TFMessage",), "Transforms"),
    Topic("/rosout", ("rcl_interfaces/msg/Log",), "Diagnostics"),
]


def tray_names(app):
    tray = app.query_one("#selected-topics", SelectedTopics)
    return {tray.get_option_at_index(i).id for i in range(tray.option_count)}


async def test_folding_and_namespace_selection_are_separate_actions(bundle):
    app, discovery, _, _ = bundle
    discovery.topics = SENSORS
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        app.selected.clear()
        app.rebuild_topics()
        tree = app.query_one("#topics", TopicTree)
        namespace = tree.nodes["namespace:/aiformula_sensing/zed_node"]
        assert not namespace.is_expanded
        tree.move_cursor(namespace)
        tree.focus()
        # Textual schedules focus with call_later; drain it before typing.
        await pilot.pause()
        assert tree.has_focus
        await pilot.press("enter")
        assert namespace.is_expanded
        assert not app.selected
        await pilot.press("space")
        expected = {topic.name for topic in SENSORS if "/zed_node/" in topic.name}
        assert app.selected == expected
        assert tray_names(app) == expected
        await pilot.press("left")
        assert not namespace.is_expanded
        assert app.selected == expected
        assert await click_button(app, pilot, "#expand")
        assert await click_button(app, pilot, "#collapse")
        assert app.selected == expected
        assert tray_names(app) == expected


async def test_namespace_checkbox_click_selects_without_folding(bundle):
    app, discovery, _, _ = bundle
    discovery.topics = SENSORS
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        app.selected.clear()
        app.rebuild_topics()
        tree = app.query_one("#topics", TopicTree)
        node = tree.nodes["namespace:/aiformula_control"]
        expanded = node.is_expanded
        assert await pilot.click("#topics", offset=(4, node.line + 1))
        await pilot.pause()
        assert app.selected == {"/aiformula_control/game_pad/cmd_vel"}
        assert node.is_expanded == expanded
        before = set(app.selected)
        assert await pilot.click("#topics", offset=(12, node.line + 1))
        await pilot.pause()
        assert node.is_expanded != expanded
        assert app.selected == before


async def test_keyword_batches_accumulate_and_none_only_removes_matches(bundle):
    app, _, _, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        search = app.query_one("#search", Input)
        search.value = "image"
        await pilot.pause()
        assert await click_button(app, pilot, "#collapse")
        assert await click_button(app, pilot, "#all")
        assert app.selected == {"/front/image_raw", "/rear/image_raw"}
        search.value = "sensor_msgs IMU"
        await pilot.pause(0.3)
        assert app.selected == {"/front/image_raw", "/rear/image_raw"}
        assert {topic.name for topic in app.visible_topics()} == {"/imu"}
        assert await click_button(app, pilot, "#all")
        assert app.selected == {"/front/image_raw", "/rear/image_raw", "/imu"}
        search.value = "rear"
        await pilot.pause()
        assert await click_button(app, pilot, "#none")
        assert app.selected == {"/front/image_raw", "/imu"}
        search.value = "not-found"
        await pilot.pause(0.3)
        assert await click_button(app, pilot, "#none")
        assert app.selected == {"/front/image_raw", "/imu"}
        assert tray_names(app) == app.selected


async def test_selected_tray_removes_topics_hidden_by_search(bundle):
    app, _, _, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        app.selected = {"/front/image_raw", "/rear/image_raw"}
        app.rebuild_topics()
        app.query_one("#search", Input).value = "imu"
        await pilot.pause()
        tray = app.query_one("#selected-topics", SelectedTopics)
        assert tray_names(app) == app.selected
        tray.focus()
        tray.highlighted = 0
        await pilot.pause()
        assert tray.has_focus
        await pilot.press("space")
        assert app.selected == {"/rear/image_raw"}
        assert tray_names(app) == app.selected
        assert {topic.name for topic in app.visible_topics()} == {"/imu"}


async def test_search_reveals_paths_and_clearing_it_restores_folding(bundle):
    app, discovery, _, _ = bundle
    discovery.topics = SENSORS
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        tree = app.query_one("#topics", TopicTree)
        key = "namespace:/aiformula_sensing/zed_node"
        assert not tree.nodes[key].is_expanded
        selection = set(app.selected)
        app.query_one("#search", Input).value = "left_raw image"
        await pilot.pause()
        assert tree.nodes[key].is_expanded
        assert len(app.visible_topics()) == 1
        assert app.selected == selection
        app.query_one("#search", Input).value = ""
        await pilot.pause()
        assert not tree.nodes[key].is_expanded
        assert app.selected == selection


async def test_selection_updates_do_not_reset_collapsed_nodes(bundle):
    app, discovery, _, _ = bundle
    discovery.topics = SENSORS
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        tree = app.query_one("#topics", TopicTree)
        key = "namespace:/aiformula_sensing/zed_node"
        assert not tree.nodes[key].is_expanded
        assert await click_button(app, pilot, "#all")
        assert not tree.nodes[key].is_expanded
        assert set(tree.nodes[key].data.names) <= app.selected
        await choose_topic(app, pilot, SENSORS[0].name, mouse=False)
        assert tree.nodes[key].label.plain.startswith("[−]")


async def test_recording_allows_browsing_but_keeps_frozen_selection(bundle):
    app, _, recorder, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await choose_topic(app, pilot, "/front/image_raw")
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        before = set(app.selected)
        app.query_one("#search", Input).value = "imu"
        await pilot.pause()
        await choose_topic(app, pilot, "/imu", mouse=False)
        assert app.selected == before
        assert not app.query_one("#search").disabled
        assert app.query_one("#all").disabled
        assert recorder.start_calls[0][0] == sorted(before)
        await pilot.press("ctrl+t")
        recorder.finish_stop()
        app.poll_recorder()


async def test_refresh_updates_selected_offline_badge_without_dropping_selection(bundle):
    app, discovery, _, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await choose_topic(app, pilot, "/imu")
        tray = app.query_one("#selected-topics", SelectedTopics)
        assert "offline" not in tray.get_option_at_index(0).prompt.plain
        discovery.topics = [topic for topic in discovery.topics if topic.name != "/imu"]
        await app.refresh_topics()
        await pilot.pause()
        assert app.selected == {"/imu"}
        assert "offline" in tray.get_option_at_index(0).prompt.plain


@pytest.mark.parametrize("size", [(80, 24), (120, 36), (160, 45)])
async def test_tree_and_selected_tray_layout_and_visual_evidence(bundle, size):
    app, discovery, _, _ = bundle
    discovery.topics = SENSORS
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app._pending_profile = None
        app.selected = {SENSORS[0].name, SENSORS[3].name, "/tf"}
        app.query_one("#parent", Input).value = "/data/rosbags"
        app.query_one("#name", Input).value = "drive_session"
        app.notice = "Example topics · simulated resource values"
        app.rebuild_topics()
        await pilot.pause()
        tree = app.query_one("#topics", TopicTree)
        tray = app.query_one("#selected-topics", SelectedTopics)
        assert tray.region.right <= app.size.width
        assert tray.region.bottom <= app.query_one("#parent").region.y
        assert tree.region.height >= 5
        if size[0] < 110:
            assert tray.region.y >= tree.region.bottom
        else:
            assert tray.region.x >= tree.region.right
        assert tray_names(app) == app.selected
        if os.environ.get("NEOBAG_CAPTURE_UI"):
            path = Path("evidence/topic-tree")
            path.mkdir(parents=True, exist_ok=True)
            app.save_screenshot(str(path / f"tree-{size[0]}x{size[1]}.svg"))
        app.query_one("#search", Input).value = "left_raw"
        await pilot.pause()
        assert tray_names(app) == app.selected
        if os.environ.get("NEOBAG_CAPTURE_UI"):
            app.save_screenshot(str(path / f"search-{size[0]}x{size[1]}.svg"))


def test_type_palette_remains_readable_and_custom_colors_are_stable():
    def luminance(color):
        values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        values = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
        return sum(a * b for a, b in zip(values, (0.2126, 0.7152, 0.0722)))
    for background in ("#0b1220", "#243f59"):
        for color in set(TYPE_COLORS.values()) | set(PALETTE):
            assert (luminance(color) + 0.05) / (luminance(background) + 0.05) >= 4.5
    outputs = [subprocess.check_output(
        [sys.executable, "-c", "from neobag.topic_tree import type_color; print(type_color('vectornav_msgs/msg/CommonGroup'))"],
        env=dict(os.environ, PYTHONHASHSEED=seed), text=True,
    ) for seed in ("1", "2")]
    assert outputs[0] == outputs[1]


async def test_refresh_replaces_cursor_data_before_namespace_group_selection(bundle):
    app, discovery, _, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        tree = app.query_one("#topics", TopicTree)
        tree.move_cursor(tree.nodes["namespace:/front"])
        tree.focus()
        discovery.topics.append(Topic("/front/camera_info", ("sensor_msgs/msg/CameraInfo",), "Camera"))
        await app.refresh_topics()
        await pilot.pause()
        await pilot.press("space")
        assert app.selected == {"/front/image_raw", "/front/camera_info"}


async def test_topic_name_can_also_be_a_namespace(bundle):
    app, discovery, _, _ = bundle
    discovery.topics = [Topic("/camera", ("sensor_msgs/msg/CameraInfo",), "Camera"),
                        Topic("/camera/image", ("sensor_msgs/msg/Image",), "Camera")]
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        tree = app.query_one("#topics", TopicTree)
        assert tree.nodes["topic:/camera"] is not tree.nodes["namespace:/camera"]
        await choose_topic(app, pilot, "/camera", mouse=False)
        assert app.selected == {"/camera"}


async def test_resizing_behind_a_modal_preserves_selection_and_responsive_tray(bundle):
    app, _, _, _ = bundle
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await choose_topic(app, pilot, "/imu")
        await pilot.press("ctrl+l")
        await pilot.resize_terminal(80, 24)
        await pilot.press("escape")
        await pilot.pause()
        assert app.selected == {"/imu"}
        tree = app.query_one("#topics", TopicTree)
        tray = app.query_one("#selected-topics", SelectedTopics)
        assert tray.region.y >= tree.region.bottom
        await pilot.resize_terminal(160, 45)
        await pilot.pause()
        assert tray.region.x >= tree.region.right
        assert app.selected == {"/imu"}


async def test_filtering_keeps_the_selected_tray_scroll_position(bundle):
    app, discovery, _, _ = bundle
    discovery.topics = [Topic(f"/sensors/imu_{number:02d}", ("sensor_msgs/msg/Imu",), "IMU")
                        for number in range(30)]
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        assert await click_button(app, pilot, "#all")
        tray = app.query_one("#selected-topics", SelectedTopics)
        tray.scroll_end(animate=False)
        await pilot.pause()
        before = tray.scroll_y
        assert before > 0
        app.query_one("#search", Input).value = "imu_15"
        await pilot.pause()
        assert tray.scroll_y == before
        assert len(tray_names(app)) == 30

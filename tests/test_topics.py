import subprocess
from unittest.mock import patch

import pytest

from neobag.topics import CATEGORIES, Topic, TopicDiscovery, classify_topic, parse_topic_list


def test_parse_live_graph_with_hidden_topics_noise_and_multiple_types():
    output = """
Starting isolated ROS environment...
[WARN] [dds]: discovery started
/scan [sensor_msgs/msg/LaserScan]
/_hidden/status [std_msgs/msg/String]
/front/image_raw [sensor_msgs/msg/Image]
/mixed [std_msgs/msg/String, custom_msgs/msg/Status]
/mixed [custom_msgs/msg/Status]
relative/topic [std_msgs/msg/String]
/invalid-name [std_msgs/msg/String]
/missing_type []
/not_a_type [ordinary text]
/trailing [std_msgs/msg/String] diagnostic banner
"""
    topics = parse_topic_list(output)
    assert topics == [
        Topic("/front/image_raw", ("sensor_msgs/msg/Image",), "Camera"),
        Topic("/scan", ("sensor_msgs/msg/LaserScan",), "Laser"),
        Topic("/_hidden/status", ("std_msgs/msg/String",), "Diagnostics"),
        Topic("/mixed", ("custom_msgs/msg/Status", "std_msgs/msg/String"), "Other"),
    ]


@pytest.mark.parametrize(
    ("name", "message_type", "category"),
    [
        ("/anything", "sensor_msgs/msg/CameraInfo", "Camera"),
        ("/lane_line", "sensor_msgs/msg/PointCloud2", "Point clouds"),
        ("/anything", "sensor_msgs/msg/MultiEchoLaserScan", "Laser"),
        ("/anything", "sensor_msgs/msg/Imu", "IMU"),
        ("/gyro/odom", "nav_msgs/msg/Odometry", "Odometry"),
        ("/anything", "tf2_msgs/msg/TFMessage", "Transforms"),
        ("/anything", "nav_msgs/msg/Path", "Navigation"),
        ("/anything", "sensor_msgs/msg/NavSatFix", "Navigation"),
        ("/anything", "geometry_msgs/msg/Twist", "Control"),
        ("/anything", "diagnostic_msgs/msg/DiagnosticArray", "Diagnostics"),
        ("/camera/image_raw", "custom/msg/Frame", "Camera"),
        ("/sensor/points", "custom/msg/Points", "Point clouds"),
        ("/vehicle/cmd_vel", "custom/msg/Command", "Control"),
        ("/tf_static", "custom/msg/Transforms", "Transforms"),
        ("/vehicle/odom", "custom/msg/Odom", "Odometry"),
        ("/sensors/gyro", "custom/msg/Values", "IMU"),
        ("/controller/image_error", "custom/msg/Error", "Other"),
        ("/unrecognized", "std_msgs/msg/String", "Other"),
        # A concrete message type takes precedence over a suggestive path name.
        ("/camera/pose", "geometry_msgs/msg/PoseStamped", "Navigation"),
    ],
)
def test_classification(name, message_type, category):
    assert classify_topic(name, (message_type,)) == category


def test_sort_and_type_classification_are_independent_of_discovery_order():
    lines = [
        "/z [std_msgs/msg/String]",
        "/a [std_msgs/msg/String]",
        "/image [nav_msgs/msg/Path, sensor_msgs/msg/Image]",
        "/points [sensor_msgs/msg/PointCloud2]",
    ]
    assert parse_topic_list("\n".join(lines)) == parse_topic_list("\n".join(reversed(lines)))
    assert [topic.name for topic in parse_topic_list("\n".join(lines))] == ["/image", "/points", "/a", "/z"]
    assert len(CATEGORIES) == len(set(CATEGORIES))


def test_discovery_uses_wrapper_and_hidden_topics_without_daemon():
    result = subprocess.CompletedProcess([], 0, "/tf [tf2_msgs/msg/TFMessage]\n", "")
    with patch("neobag.topics.subprocess.run", return_value=result) as run:
        topics = TopicDiscovery(("micromamba", "run", "-n", "ros2-jazzy", "ros2"), timeout=8).scan()
    assert topics == [Topic("/tf", ("tf2_msgs/msg/TFMessage",), "Transforms")]
    args, kwargs = run.call_args
    assert args[0] == [
        "micromamba", "run", "-n", "ros2-jazzy", "ros2",
        "topic", "list", "-t", "--include-hidden-topics", "--no-daemon", "--spin-time", "2",
    ]
    assert kwargs["timeout"] == 8
    assert kwargs["capture_output"] is True
    assert "shell" not in kwargs


def test_empty_graph_is_empty_not_demo_data():
    with patch("neobag.topics.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
        assert TopicDiscovery().scan() == []


def test_missing_executable_explains_environment_setup():
    with patch("neobag.topics.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(RuntimeError, match=r"not found.*setup\.bash.*PATH"):
            TopicDiscovery().scan()


def test_timeout_does_not_show_a_partial_graph_as_success():
    failure = subprocess.TimeoutExpired("ros2", 5, output=b"/partial [std_msgs/msg/String]\n")
    with patch("neobag.topics.subprocess.run", side_effect=failure):
        with pytest.raises(RuntimeError, match="timed out after 5s"):
            TopicDiscovery(timeout=5).scan()


@pytest.mark.parametrize("stderr,stdout", [("No module named ros2cli", ""), ("", "ROS is not sourced")])
def test_nonzero_exit_keeps_cli_diagnostic(stderr, stdout):
    with patch("neobag.topics.subprocess.run", return_value=subprocess.CompletedProcess([], 1, stdout, stderr)):
        with pytest.raises(RuntimeError) as error:
            TopicDiscovery().scan()
    assert "exit 1" in str(error.value)
    assert (stderr or stdout) in str(error.value)
    assert "sourced ROS/workspace environment" in str(error.value)


def test_permission_error_is_actionable():
    with patch("neobag.topics.subprocess.run", side_effect=PermissionError("Permission denied")):
        with pytest.raises(RuntimeError, match="Cannot run ROS 2 topic discovery: Permission denied"):
            TopicDiscovery().scan()


@pytest.mark.parametrize("command", [(), ("",), ("ros2", ""), (None,)])
def test_invalid_commands_rejected(command):
    with pytest.raises(ValueError):
        TopicDiscovery(command)


def test_zero_timeout_rejected():
    with pytest.raises(ValueError):
        TopicDiscovery(timeout=0)

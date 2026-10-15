"""Discover the current ROS 2 graph through the installed ``ros2`` command.

The CLI also supports installations that wrap ROS in an isolated environment.
Graph discovery includes topics with subscriptions only; it is not evidence that
a publisher is currently delivering messages, nor a per-topic frequency probe.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess
from collections.abc import Sequence


CATEGORIES: tuple[str, ...] = (
    "Camera",
    "Point clouds",
    "Laser",
    "IMU",
    "Odometry",
    "Transforms",
    "Navigation",
    "Control",
    "Diagnostics",
    "Other",
)

_CATEGORY_INDEX = {category: index for index, category in enumerate(CATEGORIES)}
_TOPIC_LINE = re.compile(
    r"^(/(?:[A-Za-z_][A-Za-z0-9_]*)(?:/[A-Za-z_][A-Za-z0-9_]*)*)\s+\[([^\[\]]+)\]$"
)
_MESSAGE_TYPE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*/(?:(?:msg|srv|action)/)?[A-Za-z_][A-Za-z0-9_]*$"
)

_TYPE_CATEGORIES = {
    "sensor_msgs/msg/Image": "Camera",
    "sensor_msgs/msg/CompressedImage": "Camera",
    "sensor_msgs/msg/CameraInfo": "Camera",
    "sensor_msgs/msg/PointCloud": "Point clouds",
    "sensor_msgs/msg/PointCloud2": "Point clouds",
    "sensor_msgs/msg/LaserScan": "Laser",
    "sensor_msgs/msg/MultiEchoLaserScan": "Laser",
    "sensor_msgs/msg/Range": "Laser",
    "sensor_msgs/msg/Imu": "IMU",
    "sensor_msgs/msg/MagneticField": "IMU",
    "nav_msgs/msg/Odometry": "Odometry",
    "tf2_msgs/msg/TFMessage": "Transforms",
    "nav_msgs/msg/Path": "Navigation",
    "nav_msgs/msg/OccupancyGrid": "Navigation",
    "nav_msgs/msg/MapMetaData": "Navigation",
    "nav_msgs/msg/GridCells": "Navigation",
    "sensor_msgs/msg/NavSatFix": "Navigation",
    "sensor_msgs/msg/NavSatStatus": "Navigation",
    "geometry_msgs/msg/Pose": "Navigation",
    "geometry_msgs/msg/PoseArray": "Navigation",
    "geometry_msgs/msg/PoseStamped": "Navigation",
    "geometry_msgs/msg/PoseWithCovariance": "Navigation",
    "geometry_msgs/msg/PoseWithCovarianceStamped": "Navigation",
    "geometry_msgs/msg/Twist": "Control",
    "geometry_msgs/msg/TwistStamped": "Control",
    "geometry_msgs/msg/TwistWithCovariance": "Control",
    "geometry_msgs/msg/TwistWithCovarianceStamped": "Control",
    "ackermann_msgs/msg/AckermannDrive": "Control",
    "ackermann_msgs/msg/AckermannDriveStamped": "Control",
    "sensor_msgs/msg/Joy": "Control",
    "sensor_msgs/msg/JointState": "Control",
    "sensor_msgs/msg/JointCommand": "Control",
    "trajectory_msgs/msg/JointTrajectory": "Control",
    "diagnostic_msgs/msg/DiagnosticArray": "Diagnostics",
    "diagnostic_msgs/msg/DiagnosticStatus": "Diagnostics",
    "sensor_msgs/msg/BatteryState": "Diagnostics",
    "rcl_interfaces/msg/Log": "Diagnostics",
    "rcl_interfaces/msg/ParameterEvent": "Diagnostics",
    "rosgraph_msgs/msg/Clock": "Diagnostics",
}

# Type information wins over name heuristics. Match path/name tokens rather than
# arbitrary substrings, so names such as /controller/image_error are not cameras.
_NAME_CATEGORIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("Transforms", frozenset({"tf", "tf_static", "transforms"})),
    ("Odometry", frozenset({"odom", "odometry", "wheel_odom"})),
    ("IMU", frozenset({"imu", "gyro", "gyroscope", "accelerometer", "magnetic_field"})),
    ("Point clouds", frozenset({"points", "pointcloud", "pointcloud2", "point_cloud", "cloud"})),
    ("Laser", frozenset({"scan", "laser", "laserscan", "lidar"})),
    ("Camera", frozenset({"image", "image_raw", "image_rect", "image_rect_color", "image_color", "image_mono", "compressed", "compresseddepth", "camera_info", "camera", "depth", "rgb", "mask", "masks"})),
    ("Navigation", frozenset({"map", "costmap", "path", "plan", "pose", "goal", "gps", "gnss", "navsat", "fix", "waypoints", "trajectory"})),
    ("Control", frozenset({"cmd_vel", "command", "commands", "control", "motor", "motors", "steering", "throttle", "brake", "joy", "joint_states", "rpm", "ackermann"})),
    ("Diagnostics", frozenset({"diagnostics", "diagnostic", "rosout", "parameter_events", "clock", "battery", "status"})),
)


@dataclass(frozen=True, slots=True)
class Topic:
    name: str
    types: tuple[str, ...]
    category: str


def classify_topic(name: str, types: Sequence[str]) -> str:
    """Classify known message types first, with conservative name fallbacks."""
    matched = {_TYPE_CATEGORIES[t] for t in types if t in _TYPE_CATEGORIES}
    if matched:
        return min(matched, key=_CATEGORY_INDEX.__getitem__)
    path_tokens = frozenset(name.lower().strip("/").split("/"))
    for category, tokens in _NAME_CATEGORIES:
        if path_tokens & tokens:
            return category
    return "Other"


def parse_topic_list(output: str) -> list[Topic]:
    """Parse CLI topic lines, ignoring banners and other non-topic output.

    Repeated topic rows are merged, since one graph name can have multiple types.
    Results and types are deterministic regardless of discovery/output order.
    """
    names: dict[str, set[str]] = {}
    for line in output.splitlines():
        match = _TOPIC_LINE.fullmatch(line.strip())
        if not match:
            continue
        name, raw_types = match.groups()
        types = [part.strip() for part in raw_types.split(",")]
        if not all(_MESSAGE_TYPE.fullmatch(t) for t in types):
            continue
        names.setdefault(name, set()).update(types)
    topics = [
        Topic(name, tuple(sorted(types)), classify_topic(name, tuple(types)))
        for name, types in names.items()
    ]
    return sorted(topics, key=lambda topic: (_CATEGORY_INDEX[topic.category], topic.name))


class TopicDiscovery:
    """One bounded ROS graph scan; call from a worker, never the UI thread."""

    def __init__(
        self,
        ros2_command: Sequence[str] = ("ros2",),
        timeout: float = 12.0,
    ) -> None:
        if isinstance(ros2_command, str):
            ros2_command = (ros2_command,)
        if not ros2_command or any(not isinstance(arg, str) or not arg for arg in ros2_command):
            raise ValueError("ros2_command must contain a non-empty executable and arguments")
        if timeout <= 0:
            raise ValueError("Discovery timeout must be positive")
        self.ros2_command = tuple(ros2_command)
        self.timeout = timeout

    def scan(self) -> list[Topic]:
        """Return graph topics or raise an actionable ``RuntimeError``."""
        command = [
            *self.ros2_command,
            "topic", "list", "-t", "--include-hidden-topics",
            "--no-daemon", "--spin-time", "2",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"ROS 2 command {self.ros2_command[0]!r} was not found. "
                "Source your ROS 2 setup.bash and workspace setup.bash, or put your ROS wrapper on PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"ROS 2 topic discovery timed out after {self.timeout:g}s. "
                "Check your ROS environment, DDS/network settings and ROS_DOMAIN_ID, then refresh."
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Cannot run ROS 2 topic discovery: {exc}") from exc
        if result.returncode:
            detail = (result.stderr.strip() or result.stdout.strip() or "No error output")[-1800:]
            raise RuntimeError(
                f"ROS 2 topic discovery failed (exit {result.returncode}). "
                "Check your sourced ROS/workspace environment and ROS_DOMAIN_ID. "
                f"{detail}"
            )
        return parse_topic_list(result.stdout)

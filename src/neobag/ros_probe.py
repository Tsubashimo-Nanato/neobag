"""Executed by ROS's Python, not the UI venv; emits adaptive receiver QoS JSON."""
from __future__ import annotations

import json
import sys
import time


# Deliberately exclude Images, PointCloud2, maps, paths and arbitrary custom
# messages. Their queues can retain gigabytes. These common sensor/control
# messages normally have small payloads; strings/arrays are not byte-bounded.
BUFFERED_TYPES = {
    "sensor_msgs/msg/Imu", "sensor_msgs/msg/MagneticField", "sensor_msgs/msg/NavSatFix",
    "sensor_msgs/msg/NavSatStatus", "sensor_msgs/msg/FluidPressure", "sensor_msgs/msg/Temperature",
    "sensor_msgs/msg/TimeReference", "sensor_msgs/msg/CameraInfo", "sensor_msgs/msg/Joy",
    "sensor_msgs/msg/JointState", "nav_msgs/msg/Odometry", "tf2_msgs/msg/TFMessage",
    "can_msgs/msg/Frame", "rcl_interfaces/msg/Log", "diagnostic_msgs/msg/DiagnosticArray",
    *{"geometry_msgs/msg/" + name for name in (
        "Point", "PointStamped", "Pose", "Pose2D", "PoseStamped", "PoseWithCovariance",
        "PoseWithCovarianceStamped", "Quaternion", "QuaternionStamped", "Transform",
        "TransformStamped", "Twist", "TwistStamped", "TwistWithCovariance",
        "TwistWithCovarianceStamped", "Vector3", "Vector3Stamped", "Wrench", "WrenchStamped")},
    *{"std_msgs/msg/" + name for name in ("Bool", "Byte", "Char", "Float32", "Float64",
        "Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64", "Empty")},
    *{"vectornav_msgs/msg/" + name for name in ("CommonGroup", "GpsGroup", "ImuGroup",
        "InsGroup", "TimeGroup", "AttitudeGroup")},
}


def profile_for_offers(offers: list[dict]) -> dict | None:
    if not offers or any(offer["type"] not in BUFFERED_TYPES for offer in offers):
        return None
    reliability = {offer["reliability"] for offer in offers}
    durability = {offer["durability"] for offer in offers}
    if not reliability <= {"RELIABLE", "BEST_EFFORT"} or not durability <= {"VOLATILE", "TRANSIENT_LOCAL"}:
        return None  # Unknown vendor QoS remains ROS-adaptive.
    return {"history": "keep_last", "depth": 1000,
            "reliability": "best_effort" if "BEST_EFFORT" in reliability else "reliable",
            "durability": "transient_local" if durability == {"TRANSIENT_LOCAL"} else "volatile"}


def main():
    import rclpy
    names = json.load(sys.stdin)
    rclpy.init()
    node = rclpy.create_node("neobag_qos_probe", enable_rosout=False, start_parameter_services=False)
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        result = {}
        for name in names:
            offers = [{"type": endpoint.topic_type,
                       "reliability": endpoint.qos_profile.reliability.name,
                       "durability": endpoint.qos_profile.durability.name}
                      for endpoint in node.get_publishers_info_by_topic(name)]
            profile = profile_for_offers(offers)
            if profile:
                result[name] = profile
        print(json.dumps(result))
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()

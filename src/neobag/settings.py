"""Recorder-only defaults. Never alter publishers or the launching environment."""
from __future__ import annotations

import os
import json
import re
from pathlib import Path
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

import psutil


# Process-local receive tuning; the OS may cap the requested socket buffer.
# Async delivery keeps image deserialization out of the network receive thread.
CYCLONE_RECEIVER_CONFIG = """<CycloneDDS><Domain Id="any"><Internal>
<SocketReceiveBufferSize max="4 MiB"/>
<SynchronousDeliveryPriorityThreshold>2147483647</SynchronousDeliveryPriorityThreshold>
<DeliveryQueueMaxSamples>1024</DeliveryQueueMaxSamples>
<AckDelay>1 ms</AckDelay><NackDelay>1 ms</NackDelay>
</Internal></Domain></CycloneDDS>"""


def package_available(command, package: str) -> bool:
    try:
        result = subprocess.run([*command, "pkg", "prefix", package], capture_output=True,
                                text=True, timeout=8, check=False)
        return result.returncode == 0 and any(Path(line.strip()).is_absolute() and Path(line.strip()).is_dir()
                                            for line in result.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        return False


def cache_bytes(override_mib: int | None = None) -> int:
    if override_mib is not None:
        if (not isinstance(override_mib, int) or isinstance(override_mib, bool)
                or (override_mib != 0 and not 16 <= override_mib <= 8192)):
            raise ValueError("Cache must be 0 (disabled) or between 16 and 8192 MiB")
        return override_mib * 2**20
    try:
        memory = psutil.virtual_memory()
        # ROS uses two buffers. Keep their combined bound <= half the currently
        # available memory and <= one eighth of total RAM, capped at 1 GiB.
        return max(1, min(512 * 2**20, memory.total // 16, memory.available // 4))
    except (OSError, RuntimeError):
        return 128 * 2**20


def supports_byte_cache(command) -> bool:
    """Probe the invoked CLI: Foxy's cache is a message count, not bytes.

    Unknown implementations use synchronous writes. Inspect only this option's
    description; an unrelated max-bag-size option also mentions bytes.
    """
    try:
        result = subprocess.run([*command, "bag", "record", "--help"],
                                capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode:
        return False
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^\s*--max-cache-size(?:\s|=|$)", line):
            continue
        description = [line]
        for following in lines[index + 1:]:
            if re.match(r"^\s*-\S", following):
                break
            description.append(following)
        return bool(re.search(r"\bbytes?\b", " ".join(description), re.IGNORECASE))
    return False


def resolve_settings(command, storage: str, rmw: str, cache_mib: int | None):
    if rmw not in ("auto", "inherit", "cyclonedds", "fastrtps"):
        raise ValueError("Invalid recorder RMW choice")
    requested_cache = cache_bytes(cache_mib)
    cache = requested_cache if requested_cache and supports_byte_cache(command) else 0
    env = os.environ.copy()
    probes = []
    if storage == "auto":
        probes.append("rosbag2_storage_mcap")
    # Explicit terminal/vendor configuration always wins over auto selection.
    configured = any(env.get(key) for key in ("RMW_IMPLEMENTATION", "FASTDDS_DEFAULT_PROFILES_FILE", "FASTRTPS_DEFAULT_PROFILES_FILE"))
    choose_cyclone = rmw == "auto" and os.name == "posix" and not configured
    if choose_cyclone:
        probes.append("rmw_cyclonedds_cpp")
    with ThreadPoolExecutor(max_workers=2) as pool:
        available = dict(zip(probes, pool.map(lambda name: package_available(command, name), probes)))
    if storage == "auto":
        storage = "mcap" if available["rosbag2_storage_mcap"] else "sqlite3"
    if rmw in ("cyclonedds", "fastrtps"):
        env["RMW_IMPLEMENTATION"] = f"rmw_{rmw}_cpp"
    elif choose_cyclone and available["rmw_cyclonedds_cpp"]:
        env["RMW_IMPLEMENTATION"] = "rmw_cyclonedds_cpp"
    if (rmw == "cyclonedds" or (choose_cyclone and available["rmw_cyclonedds_cpp"])) and not env.get("CYCLONEDDS_URI"):
        env["CYCLONEDDS_URI"] = CYCLONE_RECEIVER_CONFIG
    return storage, env, cache


def buffered_qos(command, topics: list[str], environment: dict) -> dict:
    """Resolve the ROS interpreter for sourced apt installations and conda wrappers.

    No ROS packages are installed into the UI venv. An incompatible interpreter
    or opaque launcher fails visibly back to native ROS QoS at the caller.
    """
    result = subprocess.run([*command, "pkg", "prefix", "rclpy"], capture_output=True,
                            text=True, env=environment, timeout=8, check=True)
    prefixes = [Path(line.strip()) for line in result.stdout.splitlines()
                if Path(line.strip()).is_absolute() and Path(line.strip()).is_dir()]
    if not prefixes:
        raise RuntimeError("Could not find ROS rclpy prefix")
    prefix = prefixes[-1]
    candidates = [prefix / "bin/python3", prefix / "bin/python", Path("/usr/bin/python3")]
    if shutil.which("python3"):
        candidates.append(Path(shutil.which("python3")))
    interpreter = next((path for path in candidates if path.is_file()), None)
    if interpreter is None:
        raise RuntimeError("Could not find ROS Python")
    process = subprocess.run([str(interpreter), str(Path(__file__).with_name("ros_probe.py"))],
                             input=json.dumps(topics), capture_output=True, text=True,
                             env=environment, timeout=12, check=True)
    profiles = json.loads(process.stdout)
    if not isinstance(profiles, dict) or not set(profiles) <= set(topics):
        raise ValueError("Invalid ROS QoS probe response")
    return profiles

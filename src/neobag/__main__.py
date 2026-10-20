"""Command line entry point; ROS inherits the launching terminal's environment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(description="neobag — live ROS 2 terminal recorder")
    parser.add_argument("--output", type=Path, default=Path.home() / "bags", help="Parent directory for bags (default: ~/bags)")
    parser.add_argument("--name", default=None, help="Initial bag name")
    parser.add_argument("--storage", choices=("auto", "sqlite3", "mcap"), default="auto", help="Prefer installed MCAP; otherwise SQLite3")
    parser.add_argument("--recorder-rmw", choices=("auto", "inherit", "cyclonedds", "fastrtps"), default="auto", help="Recorder only: auto honors explicit DDS settings, otherwise prefers installed Cyclone DDS")
    parser.add_argument("--cache-mib", type=int, help="0 disables cache; 16–8192 MiB per buffer on byte-cache ROS. Foxy/unknown implementations use direct writes")
    parser.add_argument("--qos-overrides", type=Path, help="Optional rosbag QoS YAML for selected topics (overrides automatic receiver buffering)")
    parser.add_argument("--native-qos", action="store_true", help="Disable automatic buffering for small sensor/control messages")
    parser.add_argument("--doctor", action="store_true", help="Check ROS discovery and local resources, then exit")
    parser.add_argument("--version", action="version", version=f"neobag {__version__}")
    args = parser.parse_args()
    if args.cache_mib is not None and args.cache_mib != 0 and not 16 <= args.cache_mib <= 8192:
        parser.error("--cache-mib must be 0 (disabled) or between 16 and 8192")
    if args.doctor:
        from dataclasses import asdict
        from .topics import TopicDiscovery
        from .resources import ResourceMonitor
        result = {"resources": asdict(ResourceMonitor().sample(args.output))}
        try:
            result["topics"] = [asdict(t) for t in TopicDiscovery().scan()]
        except RuntimeError as exc:
            result["error"] = str(exc)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            raise SystemExit(1)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    from .app import NeoBagApp
    from .recorder import Recorder
    app = NeoBagApp(output_parent=args.output, bag_name=args.name, storage=args.storage,
                   recorder=Recorder(qos_overrides=args.qos_overrides, rmw=args.recorder_rmw, cache_mib=args.cache_mib,
                                     buffer_qos=not args.native_qos))
    if args.qos_overrides:
        app.notice = f"Custom QoS: {args.qos_overrides}. Choose topics to record."
    try:
        app.run()
    finally:
        app.finish_cleanup()


if __name__ == "__main__":
    main()

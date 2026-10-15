"""Command line entry point; ROS inherits the launching terminal's environment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="neobag — live ROS 2 terminal recorder")
    parser.add_argument("--output", type=Path, default=Path.home() / "bags", help="Parent directory for bags (default: ~/bags)")
    parser.add_argument("--name", default=None, help="Initial bag name")
    parser.add_argument("--storage", choices=("sqlite3", "mcap"), default="sqlite3")
    parser.add_argument("--qos-overrides", type=Path, help="Optional rosbag QoS YAML for selected topics (default: ROS adaptive QoS)")
    parser.add_argument("--doctor", action="store_true", help="Check ROS discovery and local resources, then exit")
    parser.add_argument("--version", action="version", version="neobag 0.1.0")
    args = parser.parse_args()
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
                   recorder=Recorder(qos_overrides=args.qos_overrides))
    if args.qos_overrides:
        app.notice = f"Custom QoS: {args.qos_overrides}. Choose topics to record."
    try:
        app.run()
    finally:
        app.finish_cleanup()


if __name__ == "__main__":
    main()

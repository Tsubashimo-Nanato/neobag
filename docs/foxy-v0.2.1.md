# neobag 0.2.1: Foxy recording and manual refresh

## Problem and change

The previous 0.2.0 deployment on ROS 2 Foxy / rosbag2 0.3.11 exposed three
separate problems:

- Every five seconds, automatic direct topic discovery started a ROS CLI
  process that consumed roughly nine to ten CPU cores on this Jetson.
- Foxy's `--max-cache-size` is a message count. Passing the modern byte-sized
  default as 536,870,912 left a 30-second recording with 455 metadata entries
  but zero actual SQLite rows.
- Foxy's ros2cli can return 2 after a requested SIGINT. A second recording
  with direct writes contained 442 real images, but the UI reported failure.

Version 0.2.1 scans topics once at startup and then only on Refresh / Ctrl+R.
The one-second resource timer remains enabled. Recording preparation inspects
the invoked CLI's cache-option help instead of trusting ROS_DISTRO; only an
explicit byte-based option enables MiB caching. Legacy and unknown versions
use direct writes, including when a nonzero MiB override was requested.

After Stop, SQLite metadata and actual per-topic/total row counts are checked
read-only in a background worker. The UI stays in Stopping until validation
finishes. A requested SIGINT exit 2 is accepted only after SQLite validation;
an unsolicited exit 2, a missing database, or inconsistent counts fails.
An intentionally empty recording with consistent zero counts is valid. This
does not prove that every message emitted by a publisher was received.

Implementation sources:

- [Foxy cache option units](https://github.com/ros2/rosbag2/blob/0.3.11/ros2bag/ros2bag/verb/record.py)
- [Foxy cache write/reset implementation](https://github.com/ros2/rosbag2/blob/0.3.11/rosbag2_cpp/src/rosbag2_cpp/writers/sequential_writer.cpp)
- [Foxy SIGINT return code](https://github.com/ros2/ros2cli/blob/foxy/ros2cli/ros2cli/cli.py)

## Local regression checks

Python 3.10.20 and 3.12.13 each passed all 317 tests. These include legacy,
modern and unknown cache-help variants; explicit cache disable; real child
SIGINT handling; metadata/SQLite count disagreement; background validation
that keeps the UI responsive; and no periodic discovery after the startup
scan while the Refresh button, Ctrl+R and resource timer remain functional.

## Vehicle reproduction

The independent UI environment uses Python 3.12.15; ROS Foxy retains system
Python 3.8.10. The installed application source is pinned at
`f38cfffe4e771afb5250dcfaef27411832fa2a87`. Its Python module bytes were
checked against the source snapshot. The original managed release and launcher
backup remain available on the vehicle. No system packages were installed.

The opt-in harness uses the installed application and the actual visible
terminal interface, including its existing Refresh, Record and Stop buttons:

```bash
source /opt/ros/foxy/setup.bash
source /home/workspace/install/setup.bash
/path/to/installed/neobag/venv/bin/python scripts/vehicle_acceptance.py \
  --output '/media/control/Extreme SSD/rosbag' \
  --evidence /path/to/new-evidence-directory \
  --name unique_capture_name --seconds 30
```

It uses existing left-undistorted-image and cmd_vel publishers/subscriptions,
never sends velocity commands, never launches allnodes, and never replays bags.
It checks an idle interval longer than the former refresh period, explicitly
presses Refresh once, records with production defaults, waits for saving,
verifies the actual databases, and checks the idle interval again. SVG files
are Textual exports of the real running application's rendered interface.
The harness leaves the application open after completion.

The first vehicle attempt stopped before recording because allnodes had exited
and both requested topics were absent. That failed discovery receipt is
preserved; it is not a passed recording. The operator's existing authorization
to run allnodes was used to restart it before the next attempt.

## Vehicle result — 2026-10-19, 14:50–14:52 JST

The second attempt passed on the Jetson with ROS Foxy / rosbag2 0.3.11,
using existing allnodes camera publishers and the Extreme SSD. No temporary
cache monkey-patch or recorder override was used. The actual child command
automatically included `--max-cache-size 0`. Requested Stop returned 2;
the application verified SQLite and reached `finished` with no error.

| Phase | Samples | CPU mean | CPU peak |
| --- | ---: | ---: | ---: |
| Idle baseline | 12 | 38.1% | 39.6% |
| Recording | 30 | 39.7% | 45.5% |
| After saving | 12 | 37.8% | 43.4% |

The initial startup scan and explicit Refresh each produced a temporary high
CPU interval. The trace contains exactly two discovery calls across the entire
run, with no periodic scan in either 12-second idle window. Resource monitoring
continued once per second. Recording RAM averaged 23.8%; GPU averaged 86.5%.
These are whole-host values with the existing camera/perception stack running,
not neobag process-only costs or a guaranteed performance bound.

The 30.14-second recorder lifetime saved 413 real 640x360 image messages,
approximately 15 Hz over the recorded timestamps, in a 381,071,360-byte SQLite
database. Metadata total and per-topic counts exactly match the actual rows.
The selected cmd_vel topic was stored in the bag schema but had zero messages;
no input velocity command was generated for this check. This is not validation
of nonzero vehicle commands or proof of lossless recording under arbitrary load.

Saved bag on the vehicle:
`/media/control/Extreme SSD/rosbag/neobag_0_2_1_20261019_145045`.
Raw logs, discovery/resource timelines, rendered UI exports and SHA256 manifest
are in [the evidence directory](foxy-v0.2.1-evidence/). The 381 MB image bag stays
on the SSD and is not uploaded to Git. In particular:

- [Passed live result](foxy-v0.2.1-evidence/passed/result.json)
- [Saved UI export](foxy-v0.2.1-evidence/passed/saved.svg)
- [Absent-topics preflight failure](foxy-v0.2.1-evidence/missing_topics/result.json)
- [Installation and rollback receipt](foxy-v0.2.1-evidence/deployment_receipt.json)

The recorder process has exited. allnodes and the upgraded, non-recording
neobag interface are left open. To roll back the application, close its UI and
restore the launcher backup named in the deployment receipt; the previous
managed release and all bags are retained. System Python and ROS remain unchanged.

import json
from pathlib import Path
import time

import pytest

from neobag.ros_probe import profile_for_offers
from neobag.recorder import Recorder


def offer(reliability="RELIABLE", durability="VOLATILE", type="sensor_msgs/msg/Imu"):
    return dict(type=type, reliability=reliability, durability=durability)


def test_buffered_profile_matches_mixed_publishers():
    assert profile_for_offers([offer(), offer("BEST_EFFORT", "TRANSIENT_LOCAL")]) == {
        "history": "keep_last", "depth": 1000, "reliability": "best_effort", "durability": "volatile"}
    assert profile_for_offers([offer(durability="TRANSIENT_LOCAL")])["durability"] == "transient_local"


@pytest.mark.parametrize("offers", [[], [offer("UNKNOWN")], [offer(type="sensor_msgs/msg/Image")],
                                   [offer(type="sensor_msgs/msg/PointCloud2")], [offer(type="custom/msg/Huge")]])
def test_unknown_or_large_messages_keep_native_adaptation(offers):
    assert profile_for_offers(offers) is None


def test_generated_qos_lives_until_finalize_and_is_retained(tmp_path, monkeypatch):
    import sys
    script = tmp_path / "fake.py"
    script.write_text('''import sys,json,time,signal
from pathlib import Path
p=Path(sys.argv[sys.argv.index('--output')+1]);p.mkdir()
q=Path(sys.argv[sys.argv.index('--qos-profile-overrides-path')+1])
(p/'qos-path').write_text(str(q))
(p/'read-qos.json').write_text(q.read_text())
(p/'metadata.yaml').write_text('finalized')
''')
    profiles = {"/imu": profile_for_offers([offer()])}
    monkeypatch.setattr("neobag.recorder.buffered_qos", lambda *args: profiles)
    monkeypatch.setattr("neobag.recorder.resolve_settings", lambda *args: ("sqlite3", __import__('os').environ.copy(), 1024))
    recorder = Recorder(ros2_command=(sys.executable, str(script)))
    output = recorder.start(["/imu"], tmp_path, "bag")
    deadline = time.monotonic() + 5
    while recorder.is_active and time.monotonic() < deadline:
        time.sleep(0.02)
    recorder.poll()
    assert json.loads((output / "read-qos.json").read_text()) == profiles
    assert json.loads((output / "neobag-receiver-qos.yaml").read_text()) == profiles
    assert not Path((output / "qos-path").read_text()).exists()


def test_transport_loss_count_survives_log_drain_and_bounded_queue():
    import io
    from collections import deque
    recorder = Recorder(max_log_lines=1)
    recorder._read_logs(io.StringIO("[WARN] Number of messages lost on the transport layer: 38\nother line\n"), recorder._logs)
    assert recorder.drain_logs() == ["other line"]
    assert recorder.transport_lost == 38

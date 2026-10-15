"""CLI configuration must reach the recorder and retain cleanup on UI failure."""
import sys

import pytest

from neobag.__main__ import main


def test_custom_qos_reaches_recorder_and_cleanup(monkeypatch, tmp_path):
    calls = {}
    qos = tmp_path / "qos.yaml"
    qos.write_text("/imu: {history: keep_last, depth: 1000, reliability: reliable, durability: volatile}\n")

    class Recorder:
        def __init__(self, **kwargs):
            calls["recorder"] = kwargs

    class App:
        def __init__(self, **kwargs):
            calls["app"] = kwargs

        def run(self):
            calls["notice"] = self.notice
            raise RuntimeError("terminal disconnected")

        def finish_cleanup(self):
            calls["cleaned"] = True

    monkeypatch.setattr("neobag.recorder.Recorder", Recorder)
    monkeypatch.setattr("neobag.app.NeoBagApp", App)
    monkeypatch.setattr(sys, "argv", ["neobag", "--qos-overrides", str(qos), "--output", str(tmp_path), "--name", "capture"])
    with pytest.raises(RuntimeError, match="terminal disconnected"):
        main()
    assert calls["recorder"] == {"qos_overrides": qos}
    assert calls["app"]["output_parent"] == tmp_path
    assert calls["app"]["bag_name"] == "capture"
    assert str(qos) in calls["notice"]
    assert calls["cleaned"]

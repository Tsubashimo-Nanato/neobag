"""Pure acceptance-policy tests; no ROS process, bag, or load is launched."""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path
import sys
import time

import pytest

from neobag.resources import ResourceSnapshot


@pytest.fixture(scope="module")
def acceptance_module():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    spec = importlib.util.spec_from_file_location("neobag_replay_acceptance_test", scripts / "replay_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(scripts))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scripts))
    return module


def topic(source=10, destination=10, missing=0, extra=0, *, type_match=True, exact=True):
    return {
        "source_count": source,
        "destination_count": destination,
        "missing_count": missing,
        "extra_count": extra,
        "type_match": type_match,
        "exact_payload_match": exact,
    }


def comparison(topics):
    return {"topics": topics, "missing_count": sum(item["missing_count"] for item in topics.values())}


def test_complete_sensor_payloads_accept_known_system_extras(acceptance_module):
    report = comparison({
        "/camera/image_raw": topic(),
        "/imu": topic(),
        "/rosout": topic(8, 12, extra=4, exact=False),
        "/parameter_events": topic(0, 6, extra=6, exact=False),
        "/rosbag2_player/events/read_split": topic(0, 1, extra=1, type_match=False, exact=False),
        "/events/read_split": topic(0, 1, extra=1, type_match=False, exact=False),
    })
    assert acceptance_module.evaluate_comparison(report) == {
        "accepted": True,
        "sensor_exact": True,
        "no_missing": True,
        "unexpected_extra_topics": {},
        "allowed_system_extra_count": 12,
    }


def test_absent_zero_count_metadata_only_topic_does_not_fail_payload_acceptance(acceptance_module):
    report = comparison({
        "/imu": topic(),
        "/clicked_point": topic(0, 0, type_match=False, exact=False),
    })
    assert acceptance_module.evaluate_comparison(report)["accepted"] is True


@pytest.mark.parametrize("name", ["/imu", "/rosout", "/parameter_events"])
def test_any_missing_payload_fails_even_for_system_topic(acceptance_module, name):
    report = comparison({name: topic(10, 9, missing=1, exact=False)})
    result = acceptance_module.evaluate_comparison(report)
    assert result["accepted"] is False
    assert result["no_missing"] is False


def test_inconsistent_global_missing_zero_cannot_hide_topic_loss(acceptance_module):
    report = comparison({"/imu": topic(10, 9, missing=1, exact=False)})
    report["missing_count"] = 0
    assert acceptance_module.evaluate_comparison(report)["accepted"] is False


@pytest.mark.parametrize("name,source", [
    ("/camera/image_raw", 10),
    ("/new_sensor", 0),
    ("/driver/rosout", 0),
    ("/rosbag2_player/events/unrecognized", 0),
    ("/events/unrecognized", 0),
])
def test_extra_sensor_or_unrecognized_diagnostic_payload_fails(acceptance_module, name, source):
    report = comparison({name: topic(source, source + 2, extra=2, exact=False)})
    result = acceptance_module.evaluate_comparison(report)
    assert result["accepted"] is False
    assert result["no_missing"] is True
    assert result["unexpected_extra_topics"] == {name: 2}
    assert result["allowed_system_extra_count"] == 0


@pytest.mark.parametrize("type_match,exact", [(False, True), (True, False), (False, False)])
def test_sensor_type_or_serialization_mismatch_fails_without_count_difference(acceptance_module, type_match, exact):
    # compare_bags exposes serialization mismatch through exact_payload_match.
    report = comparison({"/imu": topic(type_match=type_match, exact=exact)})
    result = acceptance_module.evaluate_comparison(report)
    assert result["accepted"] is False
    assert result["sensor_exact"] is False


CPU_WARNING = "Sustained CPU pressure (96%): recording rate may fall or messages may be dropped."


def sample(when, *warnings, cpu=20.0, visible=None):
    return {"monotonic": when, "warnings": list(warnings), "cpu_percent": cpu,
            "visible_warning": visible if visible is not None else " | ".join(warnings) or "Resources normal"}


def pressure_samples():
    return [sample(11, cpu=91), sample(12, cpu=94), sample(13, CPU_WARNING, cpu=96)]


def test_cpu_alert_during_workload_and_later_clear_are_accepted(acceptance_module):
    result = acceptance_module.evaluate_pressure([
        sample(5), *pressure_samples(), sample(26),
    ], 10, 25)
    assert result == {"during_workload_cpu_alert": True, "post_workload_recovered": True}


def test_pre_workload_cpu_alert_cannot_satisfy_stress_acceptance(acceptance_module):
    result = acceptance_module.evaluate_pressure([
        sample(5, CPU_WARNING, cpu=96), sample(6, CPU_WARNING, cpu=96), sample(7, CPU_WARNING, cpu=96), sample(12), sample(26),
    ], 10, 25)
    assert result == {"during_workload_cpu_alert": False, "post_workload_recovered": False}


def test_ram_or_gpu_warning_does_not_substitute_for_cpu_pressure(acceptance_module):
    result = acceptance_module.evaluate_pressure([
        sample(11, cpu=96), sample(12, cpu=96),
        sample(13, "Sustained RAM pressure (96%)", "Sustained GPU pressure (99%)", cpu=96), sample(26),
    ], 10, 25)
    assert result["during_workload_cpu_alert"] is False


def test_recovery_must_follow_workload_end(acceptance_module):
    result = acceptance_module.evaluate_pressure([
        *pressure_samples(), sample(18), sample(26, CPU_WARNING, cpu=96),
    ], 10, 25)
    assert result == {"during_workload_cpu_alert": True, "post_workload_recovered": False}


def test_other_resource_warnings_do_not_prevent_cpu_recovery(acceptance_module):
    result = acceptance_module.evaluate_pressure([
        *pressure_samples(), sample(26, "Low output disk space"),
    ], 10, 25)
    assert result["post_workload_recovered"] is True


@pytest.mark.parametrize("start,end", [(None, None), (10, None), (None, 25), (25, 10), (10, 10), (float("nan"), 25), (10, float("inf"))])
def test_unstarted_or_invalid_workload_cannot_pass(acceptance_module, start, end):
    result = acceptance_module.evaluate_pressure([*pressure_samples(), sample(26)], start, end)
    assert result == {"during_workload_cpu_alert": False, "post_workload_recovered": False}


def test_no_samples_cannot_pass_pressure_check(acceptance_module):
    assert acceptance_module.evaluate_pressure([], 10, 25) == {
        "during_workload_cpu_alert": False, "post_workload_recovered": False,
    }


@pytest.mark.parametrize("cpu", [None, 89.9, float("nan"), float("inf"), 101, -1, True])
def test_warning_cannot_replace_three_real_high_cpu_samples(acceptance_module, cpu):
    samples = [sample(11, CPU_WARNING, cpu=96), sample(12, CPU_WARNING, cpu=cpu),
               sample(13, CPU_WARNING, cpu=96), sample(26)]
    assert acceptance_module.evaluate_pressure(samples, 10, 25)["during_workload_cpu_alert"] is False


def test_only_two_high_cpu_samples_cannot_pass(acceptance_module):
    samples = [sample(11, CPU_WARNING, cpu=96), sample(12, CPU_WARNING, cpu=96), sample(26)]
    assert acceptance_module.evaluate_pressure(samples, 10, 25)["during_workload_cpu_alert"] is False


def test_workload_streak_cannot_include_pre_workload_samples(acceptance_module):
    samples = [sample(8, CPU_WARNING, cpu=96), sample(9, CPU_WARNING, cpu=96),
               sample(11, CPU_WARNING, cpu=96), sample(26)]
    assert acceptance_module.evaluate_pressure(samples, 10, 25)["during_workload_cpu_alert"] is False


@pytest.mark.parametrize("visible", ["", "Resources normal", "RAM pressure"])
def test_monitor_warning_must_appear_in_rendered_ui(acceptance_module, visible):
    samples = pressure_samples()
    samples[-1]["visible_warning"] = visible
    assert acceptance_module.evaluate_pressure([*samples, sample(26)], 10, 25)["during_workload_cpu_alert"] is False


@pytest.mark.parametrize("cpu", [None, float("nan"), float("inf"), -1, 90, 99])
def test_unknown_or_high_cpu_does_not_count_as_recovery(acceptance_module, cpu):
    result = acceptance_module.evaluate_pressure([*pressure_samples(), sample(26, cpu=cpu)], 10, 25)
    assert result == {"during_workload_cpu_alert": True, "post_workload_recovered": False}


def test_stale_cpu_warning_still_visible_prevents_recovery(acceptance_module):
    samples = [*pressure_samples(), sample(26, cpu=20, visible=CPU_WARNING)]
    assert acceptance_module.evaluate_pressure(samples, 10, 25)["post_workload_recovered"] is False


def test_missing_rendered_widget_text_prevents_recovery(acceptance_module):
    recovery = sample(26)
    recovery.pop("visible_warning")
    assert acceptance_module.evaluate_pressure([*pressure_samples(), recovery], 10, 25)["post_workload_recovered"] is False


def test_svg_evidence_uses_only_visible_text(acceptance_module, tmp_path):
    path = tmp_path / "evidence.svg"
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg"><!-- CPU pressure -->'
                    '<metadata>CPU pressure</metadata><text>CPU 20%</text></svg>', encoding="utf-8")
    assert acceptance_module.screenshot_text(path) == "CPU 20%"
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>Sustained&#160;CPU&#160;pressure</text></svg>', encoding="utf-8")
    assert "CPU pressure" in acceptance_module.screenshot_text(path)


@pytest.mark.asyncio
async def test_capture_screenshots_follow_measured_workload_and_recovery(acceptance_module, tmp_path):
    class ManualCapture(acceptance_module.CaptureApp):
        def on_mount(self):
            pass  # No ROS discovery, timers, monitor reads, or workloads in this test.

    app = ManualCapture(tmp_path, output_parent=tmp_path, bag_name="test")
    high = ResourceSnapshot(cpu_percent=96, ram_percent=30, ram_used_bytes=3 * 2**30,
                            ram_total_bytes=10 * 2**30, gpu_percent=None, gpu_source="N/A",
                            disk_free_bytes=50 * 2**30, disk_percent=40, warnings=(CPU_WARNING,))
    try:
        async with app.run_test(size=(140, 42)) as pilot:
            for _ in range(3):
                app.show_resources(high)
            await pilot.pause()
            assert not app.pressure_shown
            assert not (tmp_path / "pressure.svg").exists()
            app.pressure_start = time.monotonic()
            for _ in range(3):
                app.show_resources(high)
            await pilot.pause()
            assert app.pressure_shown
            assert "CPU pressure" in acceptance_module.screenshot_text(tmp_path / "pressure.svg")
            app.show_resources(replace(high, cpu_percent=20, warnings=()))
            await pilot.pause()
            assert not app.pressure_cleared  # Worker interval has not ended.
            app.pressure_end = time.monotonic()
            app.show_resources(replace(high, cpu_percent=None, warnings=()))
            await pilot.pause()
            assert not app.pressure_cleared  # Missing telemetry is not recovery.
            app.show_resources(replace(high, cpu_percent=20, warnings=()))
            await pilot.pause()
            assert app.pressure_cleared
            assert "CPU pressure" not in acceptance_module.screenshot_text(tmp_path / "recovered.svg")
            assert app.pressure_start <= app.pressure_capture_monotonic <= app.pressure_end
            assert app.recovery_capture_monotonic > app.pressure_end
            assert all(acceptance_module.evaluate_pressure(app.samples, app.pressure_start, app.pressure_end).values())
    finally:
        app.finish_cleanup()

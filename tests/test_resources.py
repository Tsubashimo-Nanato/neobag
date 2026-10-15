"""Resource pressure, unit conversion, failure and recovery contracts."""

from collections import namedtuple
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from neobag import resources
from neobag.resources import ResourceMonitor


CpuTimes = namedtuple("CpuTimes", "user nice system idle iowait guest guest_nice", defaults=(0,) * 7)


@pytest.fixture
def host(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(resources.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(resources.shutil, "which", lambda name: None)
    monkeypatch.setattr(resources.psutil, "cpu_times", lambda: CpuTimes(user=10, idle=90))
    memory = SimpleNamespace(percent=40.0, used=4 * 1024**3, total=10 * 1024**3)
    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: memory)
    disk = SimpleNamespace(free=50 * 1024**3, percent=50.0)
    monkeypatch.setattr(resources.psutil, "disk_usage", lambda path: disk)
    return SimpleNamespace(clock=clock, root=tmp_path / "sys", output=tmp_path, memory=memory, disk=disk)


def put(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")
    return path


def test_cpu_warmup_and_guest_iowait_accounting(host, monkeypatch):
    samples = iter([
        CpuTimes(user=100, idle=100, iowait=10, guest=20),
        CpuTimes(user=120, idle=160, iowait=30, guest=30),
    ])
    monkeypatch.setattr(resources.psutil, "cpu_times", lambda: next(samples))
    monitor = ResourceMonitor(sysfs_root=host.root)
    assert monitor.sample(host.output).cpu_percent is None
    # 20 busy + 60 idle + 20 iowait = 100; guest is already in user.
    assert monitor.sample(host.output).cpu_percent == 20.0


def test_cpu_counter_reset_and_no_elapsed_sample(host, monkeypatch):
    samples = iter([CpuTimes(user=100, idle=100), CpuTimes(user=20, idle=110), CpuTimes(user=20, idle=110)])
    monkeypatch.setattr(resources.psutil, "cpu_times", lambda: next(samples))
    monitor = ResourceMonitor(sysfs_root=host.root)
    assert monitor.sample(host.output).cpu_percent is None
    assert monitor.sample(host.output).cpu_percent == 0.0
    assert monitor.sample(host.output).cpu_percent is None


@pytest.mark.parametrize("relative", [
    "devices/gpu.0/load",
    "class/devfreq/17000000.gpu/load",
    "devices/platform/17000000.ga10b/load",
])
@pytest.mark.parametrize("raw, expected", [("95", 9.5), ("0", 0.0), ("1000", 100.0)])
def test_jetson_permille_including_small_values(host, relative, raw, expected):
    put(host.root, relative, raw)
    snapshot = ResourceMonitor(sysfs_root=host.root).sample(host.output)
    assert snapshot.gpu_percent == expected
    assert snapshot.gpu_source == "Jetson sysfs"


def test_amd_uses_percent_and_reports_busiest_gpu(host):
    put(host.root, "class/drm/card0/device/gpu_busy_percent", "42")
    put(host.root, "class/drm/card1/device/gpu_busy_percent", "91")
    snapshot = ResourceMonitor(sysfs_root=host.root).sample(host.output)
    assert snapshot.gpu_percent == 91.0
    assert snapshot.gpu_source == "AMD sysfs (max of 2 GPUs)"


@pytest.mark.parametrize("raw", ["N/A", "NaN", "inf", "-1", "1001", "garbage"])
def test_invalid_jetson_reading_is_unknown_not_zero(host, raw):
    put(host.root, "devices/gpu.0/load", raw)
    snapshot = ResourceMonitor(sysfs_root=host.root).sample(host.output)
    assert snapshot.gpu_percent is None
    assert snapshot.gpu_source.startswith("N/A")


def test_discovery_ignores_non_gpu_devfreq(host):
    put(host.root, "class/devfreq/15340000.vic/load", "1000")
    assert ResourceMonitor(sysfs_root=host.root).sample(host.output).gpu_percent is None


def test_nvidia_query_is_bounded_no_shell_and_busiest_valid_gpu(host, monkeypatch):
    monkeypatch.setattr(resources.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="N/A\n23\n87\n")

    monkeypatch.setattr(resources.subprocess, "run", run)
    snapshot = ResourceMonitor(sysfs_root=host.root).sample(host.output)
    assert snapshot.gpu_percent == 87
    assert snapshot.gpu_source == "nvidia-smi (max of 2 GPUs)"
    command, kwargs = calls[0]
    assert command == ["/usr/bin/nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"]
    assert 0 < kwargs["timeout"] <= 1
    assert not kwargs.get("shell", False)


def test_timed_out_gpu_probe_is_cached_and_recovers(host, monkeypatch):
    monkeypatch.setattr(resources.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    attempts = []

    def run(command, **kwargs):
        attempts.append(command)
        if len(attempts) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="15\n")

    monkeypatch.setattr(resources.subprocess, "run", run)
    monitor = ResourceMonitor(sysfs_root=host.root)
    first = monitor.sample(host.output)
    assert first.gpu_percent is None
    assert "timed out" in first.gpu_source
    host.clock[0] += 1
    assert monitor.sample(host.output).gpu_percent is None
    assert len(attempts) == 1
    host.clock[0] += 30
    assert monitor.sample(host.output).gpu_percent == 15
    assert len(attempts) == 2


def test_missing_backend_is_not_rediscovered_every_tick(host, monkeypatch):
    attempts = []
    monkeypatch.setattr(resources.shutil, "which", lambda name: attempts.append(name))
    monitor = ResourceMonitor(sysfs_root=host.root)
    for _ in range(4):
        assert monitor.sample(host.output).gpu_percent is None
        host.clock[0] += 1
    assert attempts == ["nvidia-smi"]


def test_failed_nvidia_falls_back_to_amd(host, monkeypatch):
    monkeypatch.setattr(resources.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(resources.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""))
    put(host.root, "class/drm/card0/device/gpu_busy_percent", "33")
    assert ResourceMonitor(sysfs_root=host.root).sample(host.output).gpu_percent == 33.0


def test_read_failure_does_not_reuse_stale_gpu_value(host):
    path = put(host.root, "devices/gpu.0/load", "930")
    monitor = ResourceMonitor(sysfs_root=host.root)
    assert monitor.sample(host.output).gpu_percent == 93
    path.unlink()
    assert monitor.sample(host.output).gpu_percent is None


def test_pressure_requires_consecutive_samples_and_resets(host, monkeypatch):
    host.memory.percent = 95
    path = put(host.root, "devices/gpu.0/load", "970")
    samples = iter(CpuTimes(user=95 * tick, idle=5 * tick) for tick in range(10))
    monkeypatch.setattr(resources.psutil, "cpu_times", lambda: next(samples))
    monitor = ResourceMonitor(sysfs_root=host.root)
    assert monitor.sample(host.output).warnings == ()
    assert monitor.sample(host.output).warnings == ()
    third = monitor.sample(host.output)
    assert len(third.warnings) == 2  # RAM/GPU; CPU had one warm-up sample.
    fourth = monitor.sample(host.output)
    assert len(fourth.warnings) == 3
    assert all("may" in warning and "messages may be dropped" in warning for warning in fourth.warnings)
    host.memory.percent = 40
    path.write_text("100", encoding="ascii")
    assert len(monitor.sample(host.output).warnings) == 1
    host.memory.percent = 95
    path.write_text("970", encoding="ascii")
    assert len(monitor.sample(host.output).warnings) == 1  # fresh three-sample streak needed


@pytest.mark.parametrize("free, percent", [(1024**3, 80), (10 * 1024**3, 96)])
def test_low_disk_warns_immediately(host, free, percent):
    host.disk.free, host.disk.percent = free, percent
    snapshot = ResourceMonitor(sysfs_root=host.root).sample(host.output)
    assert len(snapshot.warnings) == 1
    assert "Low output disk space" in snapshot.warnings[0]
    assert "may" in snapshot.warnings[0]


def test_output_disk_uses_existing_ancestor_without_creating_directories(host, monkeypatch):
    paths = []

    def disk_usage(path):
        paths.append(path)
        return host.disk

    monkeypatch.setattr(resources.psutil, "disk_usage", disk_usage)
    target = host.output / "new" / "bags"
    snapshot = ResourceMonitor(sysfs_root=host.root).sample(target)
    assert paths == [str(host.output)]
    assert snapshot.disk_free_bytes == host.disk.free
    assert not target.exists()


def test_unavailable_ram_cpu_and_disk_remain_unknown(host, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(resources.psutil, "virtual_memory", denied)
    monkeypatch.setattr(resources.psutil, "cpu_times", denied)
    monkeypatch.setattr(resources.psutil, "disk_usage", denied)
    snapshot = ResourceMonitor(sysfs_root=host.root).sample(host.output)
    assert snapshot.cpu_percent is None
    assert snapshot.ram_percent is None
    assert snapshot.ram_used_bytes is None
    assert snapshot.ram_total_bytes is None
    assert snapshot.disk_free_bytes is None
    assert "disk space is unavailable" in snapshot.warnings[0]

"""Host resource telemetry; pressure warnings are risks, not loss measurements.

Call ``sample`` serially, roughly once per second, outside the UI event loop.
No threads, persistent subprocesses, root permissions, or GUI are required.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import shutil
import subprocess
import time

import psutil


@dataclass(frozen=True)
class ResourceSnapshot:
    cpu_percent: float | None
    ram_percent: float | None
    ram_used_bytes: int | None
    ram_total_bytes: int | None
    gpu_percent: float | None
    gpu_source: str
    disk_free_bytes: int | None
    disk_percent: float | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _GpuBackend:
    kind: str
    paths: tuple[Path, ...] = ()
    executable: str | None = None


def _percentage(value: object, scale: float = 1.0) -> float | None:
    """Reject invalid telemetry rather than displaying it as an idle device."""
    try:
        number = float(value) / scale
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


class ResourceMonitor:
    """System-wide use with conservative, configurable pressure thresholds.

    GPU values represent the busiest readable GPU of the selected backend.
    Backend discovery is cached; missing/failed probes retry after 30 seconds.
    Jetson ``load`` is always permille (including values below 100), whereas
    AMD ``gpu_busy_percent`` and NVIDIA utilization are already percentages.
    """

    def __init__(
        self,
        *,
        high_usage_percent: float = 90.0,
        sustained_samples: int = 3,
        low_disk_bytes: int = 5 * 1024**3,
        disk_used_warning_percent: float = 95.0,
        gpu_timeout: float = 0.4,
        gpu_retry_seconds: float = 30.0,
        sysfs_root: Path = Path("/sys"),
    ) -> None:
        if not 0 < high_usage_percent <= 100:
            raise ValueError("high_usage_percent must be in (0, 100]")
        if sustained_samples < 1 or low_disk_bytes < 0:
            raise ValueError("sustained_samples must be positive and low_disk_bytes nonnegative")
        if not 0 < disk_used_warning_percent <= 100:
            raise ValueError("disk_used_warning_percent must be in (0, 100]")
        if not 0 < gpu_timeout <= 5 or not math.isfinite(gpu_retry_seconds) or gpu_retry_seconds <= 0:
            raise ValueError("GPU timeout must be in (0, 5] and retry interval must be positive")
        self.high_usage_percent = high_usage_percent
        self.sustained_samples = sustained_samples
        self.low_disk_bytes = low_disk_bytes
        self.disk_used_warning_percent = disk_used_warning_percent
        self.gpu_timeout = gpu_timeout
        self.gpu_retry_seconds = gpu_retry_seconds
        self.sysfs_root = Path(sysfs_root)
        self._previous_cpu: dict[str, float] | None = None
        self._pressure_counts = {"CPU": 0, "RAM": 0, "GPU": 0}
        self._gpu_backend: _GpuBackend | None = None
        self._gpu_probe_after = 0.0
        self._gpu_unavailable_source = "N/A (no supported GPU telemetry)"

    def sample(self, output_parent: Path) -> ResourceSnapshot:
        """Read current resources without creating or modifying output paths.

        The first CPU sample has no baseline and returns ``None``. Disk data
        refers to the nearest existing parent of the requested output path.
        Unreadable metrics remain ``None``; GPU absence is normal on some hosts.
        """
        cpu = self._sample_cpu()
        ram = ram_used = ram_total = None
        try:
            memory = psutil.virtual_memory()
            ram = _percentage(memory.percent)
            ram_used, ram_total = int(memory.used), int(memory.total)
        except (OSError, ValueError, psutil.Error):
            pass
        gpu, gpu_source = self._sample_gpu()
        disk_free, disk_percent = self._sample_disk(output_parent)
        warnings: list[str] = []
        for label, value in (("CPU", cpu), ("RAM", ram), ("GPU", gpu)):
            if value is not None and value >= self.high_usage_percent:
                self._pressure_counts[label] += 1
            else:
                self._pressure_counts[label] = 0
            if self._pressure_counts[label] >= self.sustained_samples:
                warnings.append(
                    f"Sustained {label} pressure ({value:.0f}%): "
                    "recording rate may fall or messages may be dropped."
                )
        if disk_free is not None and (
            disk_free < self.low_disk_bytes
            or (disk_percent is not None and disk_percent >= self.disk_used_warning_percent)
        ):
            warnings.append(
                f"Low output disk space ({disk_free / 1024**3:.1f} GiB free): "
                "recording may stop; recording rate may fall or messages may be dropped."
            )
        elif disk_free is None:
            warnings.append("Output disk space is unavailable; check the output location before recording.")
        return ResourceSnapshot(
            cpu_percent=cpu,
            ram_percent=ram,
            ram_used_bytes=ram_used,
            ram_total_bytes=ram_total,
            gpu_percent=gpu,
            gpu_source=gpu_source,
            disk_free_bytes=disk_free,
            disk_percent=disk_percent,
            warnings=tuple(warnings),
        )

    def _sample_cpu(self) -> float | None:
        # Own the baseline: psutil.cpu_percent() keeps a separate baseline for
        # each calling thread, but a UI executor can use different worker threads.
        try:
            current = dict(psutil.cpu_times()._asdict())
        except (OSError, psutil.Error):
            self._previous_cpu = None
            return None
        previous, self._previous_cpu = self._previous_cpu, current
        if previous is None or previous.keys() != current.keys():
            return None
        deltas = {key: max(0.0, value - previous[key]) for key, value in current.items()}
        # Linux guest and guest_nice are already included in user/nice. iowait
        # is idle CPU time. These fields are absent on other supported systems.
        total = sum(value for key, value in deltas.items() if key not in {"guest", "guest_nice"})
        if total <= 0:
            return None
        idle = deltas.get("idle", 0.0) + deltas.get("iowait", 0.0)
        return round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 1)

    @staticmethod
    def _sample_disk(output_parent: Path) -> tuple[int | None, float | None]:
        try:
            location = Path(output_parent).expanduser().absolute()
            while not location.exists():
                parent = location.parent
                if parent == location:
                    return None, None
                location = parent
            usage = psutil.disk_usage(str(location))
            return int(usage.free), _percentage(usage.percent)
        except (OSError, ValueError, RuntimeError, psutil.Error):
            return None, None

    def _discover_gpu_backends(self) -> list[_GpuBackend]:
        # Match only known GPU names, never unrelated devfreq engines such as VIC.
        jetson: set[Path] = set()
        patterns = ["devices/gpu.0/load"]
        for base in ("class/devfreq", "devices", "devices/platform", "devices/platform/host1x"):
            for name in ("*gpu*", "*gp10b*", "*gv11b*", "*ga10b*", "*gk20a*"):
                patterns.append(f"{base}/{name}/load")
        for pattern in patterns:
            try:
                jetson.update(path.resolve() for path in self.sysfs_root.glob(pattern) if path.is_file())
            except (OSError, RuntimeError):
                continue
        backends: list[_GpuBackend] = []
        if jetson:
            backends.append(_GpuBackend("Jetson sysfs", tuple(sorted(jetson))))
        executable = shutil.which("nvidia-smi")
        if executable:
            backends.append(_GpuBackend("nvidia-smi", executable=executable))
        try:
            amd = tuple(sorted(self.sysfs_root.glob("class/drm/card*/device/gpu_busy_percent")))
            if amd:
                backends.append(_GpuBackend("AMD sysfs", amd))
        except OSError:
            pass
        return backends

    def _read_gpu_backend(self, backend: _GpuBackend) -> tuple[float | None, str]:
        if backend.kind == "nvidia-smi":
            command = [
                backend.executable or "nvidia-smi",
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
            ]
            try:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.gpu_timeout,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                return None, "N/A (nvidia-smi timed out)"
            except OSError:
                return None, "N/A (nvidia-smi unavailable)"
            values = [_percentage(line.strip()) for line in result.stdout.splitlines()] if result.returncode == 0 else []
        else:
            values = []
            for path in backend.paths:
                try:
                    # Jetson load is ten times the percentage; 95 means 9.5%.
                    # NVIDIA confirms the scale at:
                    # https://forums.developer.nvidia.com/t/measure-gpu-load/106980
                    with path.open(encoding="ascii") as source:
                        raw = source.read(64).strip()
                    values.append(_percentage(raw, 10.0 if backend.kind == "Jetson sysfs" else 1.0))
                except (OSError, UnicodeError):
                    continue
        valid_values = [value for value in values if value is not None]
        if not valid_values:
            return None, f"N/A ({backend.kind} unreadable)"
        source = backend.kind
        if len(valid_values) > 1:
            source += f" (max of {len(valid_values)} GPUs)"
        return max(valid_values), source

    def _sample_gpu(self) -> tuple[float | None, str]:
        now = time.monotonic()
        if self._gpu_backend is not None:
            value, source = self._read_gpu_backend(self._gpu_backend)
            if value is not None:
                return value, source
            # Never show stale healthy values after a driver failure. Avoid
            # repeatedly running a broken or hanging driver command every tick.
            self._gpu_backend = None
            self._gpu_probe_after = now + self.gpu_retry_seconds
            self._gpu_unavailable_source = source
            return None, source
        if now < self._gpu_probe_after:
            return None, self._gpu_unavailable_source
        self._gpu_probe_after = now + self.gpu_retry_seconds
        for backend in self._discover_gpu_backends():
            value, source = self._read_gpu_backend(backend)
            if value is not None:
                self._gpu_backend = backend
                return value, source
            self._gpu_unavailable_source = source
        return None, self._gpu_unavailable_source

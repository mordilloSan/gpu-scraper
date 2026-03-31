from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import os
import select
import signal
import subprocess
from collections import deque
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Protocol

from gpu_scraper.models import GPUDevice, MetricSample
from gpu_scraper.prometheus import device_metric

LOGGER = logging.getLogger(__name__)

NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_MEM = 2
NVML_TEMPERATURE_GPU = 0


class CollectionError(RuntimeError):
    """A backend failed to collect the latest metrics."""


class CollectorBackend(Protocol):
    streaming: bool

    def collect(self) -> tuple[MetricSample, ...]:
        """Collect the next sample payload for this device."""
        raise NotImplementedError

    def close(self) -> None:
        """Release resources held by this backend."""
        raise NotImplementedError


class IncrementalJsonArrayParser:
    def __init__(self) -> None:
        self._buffer = ""
        self._started = False
        self._decoder = json.JSONDecoder()

    def feed(self, chunk: str) -> list[dict[str, object]]:
        self._buffer += chunk
        objects: list[dict[str, object]] = []
        while True:
            obj = self._extract_one()
            if obj is None:
                break
            objects.append(obj)
        return objects

    def finish(self) -> None:
        remaining = self._buffer.strip()
        if remaining and remaining not in {"]", ","}:
            raise ValueError(f"incomplete intel_gpu_top JSON stream: {remaining!r}")

    def _extract_one(self) -> dict[str, object] | None:
        self._buffer = self._buffer.lstrip()
        if not self._started:
            if not self._buffer:
                return None
            if self._buffer[0] != "[":
                raise ValueError("intel_gpu_top JSON stream did not start with '['")
            self._started = True
            self._buffer = self._buffer[1:]
            self._buffer = self._buffer.lstrip()

        while self._buffer.startswith(",") or self._buffer.startswith("]"):
            self._buffer = self._buffer[1:].lstrip()

        if not self._buffer:
            return None

        try:
            payload, index = self._decoder.raw_decode(self._buffer)
        except json.JSONDecodeError:
            return None

        self._buffer = self._buffer[index:]
        if not isinstance(payload, dict):
            raise ValueError("intel_gpu_top produced a non-object JSON payload")
        return payload


class IntelSession(Protocol):
    def read_chunk(self, timeout: float) -> bytes:
        """Return the next stdout chunk or raise EOFError/TimeoutError."""
        raise NotImplementedError

    def close(self, timeout: float = 2.0) -> None:
        """Stop the session and clean up the process."""
        raise NotImplementedError


class SubprocessIntelSession:
    def __init__(self, command: list[str]) -> None:
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def read_chunk(self, timeout: float) -> bytes:
        stdout = self._process.stdout
        if stdout is None:
            raise EOFError("intel_gpu_top stdout was not captured")

        ready, _, _ = select.select([stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("timed out waiting for intel_gpu_top output")

        chunk = os.read(stdout.fileno(), 4096)
        if chunk:
            return chunk

        stderr_text = ""
        if self._process.stderr is not None:
            stderr_text = self._process.stderr.read().decode("utf-8", "replace").strip()
        code = self._process.poll()
        detail = stderr_text or "no stderr output"
        raise EOFError(f"intel_gpu_top exited with code {code}: {detail}")

    def close(self, timeout: float = 2.0) -> None:
        if self._process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._process.pid, signal.SIGTERM)
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self._process.pid, signal.SIGKILL)
                self._process.wait(timeout=1.0)

        if self._process.stdout is not None:
            self._process.stdout.close()
        if self._process.stderr is not None:
            self._process.stderr.close()


class IntelGpuTopBackend:
    streaming = True

    def __init__(
        self,
        device: GPUDevice,
        sample_interval: float,
        intel_gpu_top_bin: str = "intel_gpu_top",
        session_factory: Callable[[], IntelSession] | None = None,
    ) -> None:
        self._device = device
        self._sample_interval = sample_interval
        self._intel_gpu_top_bin = intel_gpu_top_bin
        self._session_factory = session_factory or self._build_session
        self._session: IntelSession | None = None
        self._parser = IncrementalJsonArrayParser()
        self._pending: deque[dict[str, object]] = deque()
        self._temperature_path = _find_hwmon_input(device.sysfs_path, "temp1_input")

    def collect(self) -> tuple[MetricSample, ...]:
        last_error: Exception | None = None
        for _ in range(2):
            try:
                payload = self._next_payload()
                return self._translate_payload(payload)
            except (EOFError, OSError, TimeoutError, ValueError) as exc:
                last_error = exc
                self._restart_session()
        raise CollectionError(str(last_error or "intel collection failed"))

    def close(self) -> None:
        self._close_session()

    def _next_payload(self) -> dict[str, object]:
        if self._pending:
            return self._pending.popleft()

        while True:
            session = self._ensure_session()
            chunk = session.read_chunk(max(self._sample_interval * 2.5, 1.0))
            decoded = self._parser.feed(chunk.decode("utf-8", "replace"))
            self._pending.extend(decoded)
            if self._pending:
                return self._pending.popleft()

    def _ensure_session(self) -> IntelSession:
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    def _build_session(self) -> IntelSession:
        interval_ms = max(1, int(self._sample_interval * 1000))
        command = [
            self._intel_gpu_top_bin,
            "-J",
            "-s",
            str(interval_ms),
            "-d",
            f"sys:{self._device.sysfs_path}",
        ]
        return SubprocessIntelSession(command)

    def _restart_session(self) -> None:
        self._close_session()
        self._parser = IncrementalJsonArrayParser()
        self._pending.clear()

    def _close_session(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _translate_payload(
        self, payload: dict[str, object]
    ) -> tuple[MetricSample, ...]:
        samples: list[MetricSample] = []

        frequency = _nested_float(payload, "frequency", "actual")
        if frequency is not None:
            samples.append(
                device_metric(self._device, "gpu_core_clock_megahertz", frequency)
            )

        requested_freq = _nested_float(payload, "frequency", "requested")
        if requested_freq is not None:
            samples.append(
                device_metric(
                    self._device, "gpu_intel_requested_clock_megahertz", requested_freq
                )
            )

        power_gpu = _nested_float(payload, "power", "GPU")
        if power_gpu is not None:
            samples.append(device_metric(self._device, "gpu_power_watts", power_gpu))

        package_power = _nested_float(payload, "power", "Package")
        if package_power is not None:
            samples.append(
                device_metric(
                    self._device, "gpu_intel_package_power_watts", package_power
                )
            )

        rc6 = _nested_float(payload, "rc6", "value")
        if rc6 is not None:
            samples.append(
                device_metric(self._device, "gpu_intel_rc6_ratio", rc6 / 100.0)
            )

        engines = payload.get("engines", {})
        if isinstance(engines, dict):
            for engine_name, engine_data in sorted(engines.items()):
                if not isinstance(engine_data, dict):
                    continue
                busy = _coerce_float(engine_data.get("busy"))
                if busy is None:
                    continue
                samples.append(
                    device_metric(
                        self._device,
                        "gpu_intel_engine_utilization_ratio",
                        busy / 100.0,
                        {"engine": str(engine_name)},
                    )
                )

        imc_reads = _nested_float(payload, "imc-bandwidth", "reads")
        if imc_reads is not None:
            samples.append(
                device_metric(
                    self._device,
                    "gpu_intel_imc_read_bandwidth_mebibytes_per_second",
                    imc_reads,
                )
            )

        imc_writes = _nested_float(payload, "imc-bandwidth", "writes")
        if imc_writes is not None:
            samples.append(
                device_metric(
                    self._device,
                    "gpu_intel_imc_write_bandwidth_mebibytes_per_second",
                    imc_writes,
                )
            )

        temperature = _read_scaled_value(self._temperature_path, scale=1000.0)
        if temperature is not None:
            samples.append(
                device_metric(self._device, "gpu_temperature_celsius", temperature)
            )

        return tuple(samples)


class AmdSysfsBackend:
    streaming = False

    def __init__(self, device: GPUDevice) -> None:
        self._device = device
        self._hwmon_dir = _select_amd_hwmon(device.sysfs_path)

    def collect(self) -> tuple[MetricSample, ...]:
        samples: list[MetricSample] = []
        hwmon_dir = self._hwmon_dir or _select_amd_hwmon(self._device.sysfs_path)
        self._hwmon_dir = hwmon_dir
        if hwmon_dir is None:
            raise CollectionError(
                f"no hwmon directory found for AMD GPU at {self._device.sysfs_path}"
            )

        temperature = _read_scaled_value(hwmon_dir / "temp1_input", scale=1000.0)
        if temperature is not None:
            samples.append(
                device_metric(self._device, "gpu_temperature_celsius", temperature)
            )

        power = _read_scaled_value(hwmon_dir / "power1_average", scale=1_000_000.0)
        if power is None:
            power = _read_scaled_value(hwmon_dir / "power1_input", scale=1_000_000.0)
        if power is not None:
            samples.append(device_metric(self._device, "gpu_power_watts", power))

        core_clock = _read_scaled_value(hwmon_dir / "freq1_input", scale=1_000_000.0)
        if core_clock is not None:
            samples.append(
                device_metric(self._device, "gpu_core_clock_megahertz", core_clock)
            )

        memory_clock = _read_scaled_value(hwmon_dir / "freq2_input", scale=1_000_000.0)
        if memory_clock is not None:
            samples.append(
                device_metric(self._device, "gpu_memory_clock_megahertz", memory_clock)
            )

        utilization = _read_scaled_value(
            self._device.sysfs_path / "gpu_busy_percent", scale=100.0
        )
        if utilization is not None:
            samples.append(
                device_metric(self._device, "gpu_utilization_ratio", utilization)
            )

        vram_used = _read_scaled_value(
            self._device.sysfs_path / "mem_info_vram_used", scale=1.0
        )
        if vram_used is not None:
            samples.append(
                device_metric(self._device, "gpu_memory_used_bytes", vram_used)
            )

        vram_total = _read_scaled_value(
            self._device.sysfs_path / "mem_info_vram_total", scale=1.0
        )
        if vram_total is not None:
            samples.append(
                device_metric(self._device, "gpu_memory_total_bytes", vram_total)
            )

        fan_rpm = _read_scaled_value(hwmon_dir / "fan1_input", scale=1.0)
        if fan_rpm is not None:
            samples.append(
                device_metric(self._device, "gpu_fan_speed_rpm", fan_rpm)
            )

        return tuple(samples)

    def close(self) -> None:
        return None


class NvmlError(RuntimeError):
    """An NVML call failed."""


class NvmlUtilization(ctypes.Structure):
    _fields_ = [
        ("gpu", ctypes.c_uint),
        ("memory", ctypes.c_uint),
    ]


class NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class NvmlPciInfo(ctypes.Structure):
    _fields_ = [
        ("busIdLegacy", ctypes.c_char * 16),
        ("domain", ctypes.c_uint),
        ("bus", ctypes.c_uint),
        ("device", ctypes.c_uint),
        ("pciDeviceId", ctypes.c_uint),
        ("pciSubSystemId", ctypes.c_uint),
        ("busId", ctypes.c_char * 32),
    ]


class NvmlManager:
    def __init__(self, library_path: str | None = None) -> None:
        self._library_path = library_path or "libnvidia-ml.so.1"
        self._lock = Lock()
        self._lib: ctypes.CDLL | None = None
        self._initialized = False
        self._handles_by_slot: dict[str, ctypes.c_void_p] = {}
        self._error: str | None = None

        try:
            self._load()
        except (OSError, NvmlError) as exc:
            self._error = str(exc)

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def available(self) -> bool:
        return self._error is None and self._initialized

    def close(self) -> None:
        if not self._initialized or self._lib is None:
            return
        with self._lock:
            if self._initialized:
                self._lib.nvmlShutdown()
                self._initialized = False

    def collect_for_slot(self, pci_slot: str) -> dict[str, float]:
        if self._error is not None:
            raise CollectionError(self._error)
        if not self._initialized or self._lib is None:
            raise CollectionError("NVML is not initialized")

        normalized_slot = normalize_pci_slot(pci_slot)
        handle = self._handles_by_slot.get(normalized_slot)
        if handle is None:
            raise CollectionError(f"no NVML device matched PCI slot {normalized_slot}")

        with self._lock:
            metrics: dict[str, float] = {}
            temperature = ctypes.c_uint()
            self._check(
                self._lib.nvmlDeviceGetTemperature(
                    handle, NVML_TEMPERATURE_GPU, ctypes.byref(temperature)
                )
            )
            metrics["gpu_temperature_celsius"] = float(temperature.value)

            power = ctypes.c_uint()
            self._check(self._lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(power)))
            metrics["gpu_power_watts"] = power.value / 1000.0

            graphics_clock = ctypes.c_uint()
            self._check(
                self._lib.nvmlDeviceGetClockInfo(
                    handle, NVML_CLOCK_GRAPHICS, ctypes.byref(graphics_clock)
                )
            )
            metrics["gpu_core_clock_megahertz"] = float(graphics_clock.value)

            memory_clock = ctypes.c_uint()
            self._check(
                self._lib.nvmlDeviceGetClockInfo(
                    handle, NVML_CLOCK_MEM, ctypes.byref(memory_clock)
                )
            )
            metrics["gpu_memory_clock_megahertz"] = float(memory_clock.value)

            utilization = NvmlUtilization()
            self._check(
                self._lib.nvmlDeviceGetUtilizationRates(
                    handle, ctypes.byref(utilization)
                )
            )
            metrics["gpu_utilization_ratio"] = utilization.gpu / 100.0
            metrics["gpu_memory_utilization_ratio"] = utilization.memory / 100.0

            memory_info = NvmlMemory()
            self._check(
                self._lib.nvmlDeviceGetMemoryInfo(
                    handle, ctypes.byref(memory_info)
                )
            )
            metrics["gpu_memory_used_bytes"] = float(memory_info.used)
            metrics["gpu_memory_total_bytes"] = float(memory_info.total)

            fan_speed = ctypes.c_uint()
            self._check(
                self._lib.nvmlDeviceGetFanSpeed(handle, ctypes.byref(fan_speed))
            )
            metrics["gpu_fan_speed_ratio"] = fan_speed.value / 100.0

            enc_util = ctypes.c_uint()
            enc_period = ctypes.c_uint()
            self._check(
                self._lib.nvmlDeviceGetEncoderUtilization(
                    handle, ctypes.byref(enc_util), ctypes.byref(enc_period)
                )
            )
            metrics["gpu_nvidia_encoder_utilization_ratio"] = enc_util.value / 100.0

            dec_util = ctypes.c_uint()
            dec_period = ctypes.c_uint()
            self._check(
                self._lib.nvmlDeviceGetDecoderUtilization(
                    handle, ctypes.byref(dec_util), ctypes.byref(dec_period)
                )
            )
            metrics["gpu_nvidia_decoder_utilization_ratio"] = dec_util.value / 100.0

            return metrics

    def _load(self) -> None:
        self._lib = ctypes.CDLL(self._library_path)
        self._configure_functions(self._lib)
        self._check(self._lib.nvmlInit_v2())
        self._initialized = True

        device_count = ctypes.c_uint()
        self._check(self._lib.nvmlDeviceGetCount_v2(ctypes.byref(device_count)))

        for index in range(device_count.value):
            handle = ctypes.c_void_p()
            self._check(
                self._lib.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle))
            )
            pci_info = NvmlPciInfo()
            self._check(self._lib.nvmlDeviceGetPciInfo(handle, ctypes.byref(pci_info)))
            slot = normalize_pci_slot(
                pci_info.busId.decode("utf-8", "replace").strip("\x00")
            )
            self._handles_by_slot[slot] = handle

    def _check(self, return_code: int) -> None:
        if return_code == 0:
            return
        if self._lib is None:
            raise NvmlError(f"NVML error {return_code}")
        try:
            error_string = self._lib.nvmlErrorString(return_code)
            message = error_string.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive fallback
            message = f"NVML error {return_code}"
        raise NvmlError(message)

    @staticmethod
    def _configure_functions(lib: ctypes.CDLL) -> None:
        lib.nvmlInit_v2.restype = ctypes.c_int
        lib.nvmlShutdown.restype = ctypes.c_int
        lib.nvmlDeviceGetCount_v2.restype = ctypes.c_int
        lib.nvmlDeviceGetCount_v2.argtypes = [ctypes.POINTER(ctypes.c_uint)]
        lib.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        pci_info_fn = None
        for candidate in (
            "nvmlDeviceGetPciInfo",
            "nvmlDeviceGetPciInfo_v2",
            "nvmlDeviceGetPciInfo_v3",
        ):
            pci_info_fn = getattr(lib, candidate, None)
            if pci_info_fn is not None:
                break
        if pci_info_fn is None:
            raise NvmlError(
                "NVML does not expose a supported nvmlDeviceGetPciInfo function"
            )
        pci_info_fn.restype = ctypes.c_int
        pci_info_fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(NvmlPciInfo)]
        lib.nvmlDeviceGetPciInfo = pci_info_fn  # type: ignore[attr-defined]
        lib.nvmlDeviceGetTemperature.restype = ctypes.c_int
        lib.nvmlDeviceGetTemperature.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.nvmlDeviceGetPowerUsage.restype = ctypes.c_int
        lib.nvmlDeviceGetPowerUsage.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.nvmlDeviceGetClockInfo.restype = ctypes.c_int
        lib.nvmlDeviceGetClockInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int
        lib.nvmlDeviceGetUtilizationRates.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(NvmlUtilization),
        ]
        lib.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
        lib.nvmlDeviceGetMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(NvmlMemory),
        ]
        lib.nvmlDeviceGetFanSpeed.restype = ctypes.c_int
        lib.nvmlDeviceGetFanSpeed.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.nvmlDeviceGetEncoderUtilization.restype = ctypes.c_int
        lib.nvmlDeviceGetEncoderUtilization.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.nvmlDeviceGetDecoderUtilization.restype = ctypes.c_int
        lib.nvmlDeviceGetDecoderUtilization.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.nvmlErrorString.restype = ctypes.c_char_p
        lib.nvmlErrorString.argtypes = [ctypes.c_int]


class NvidiaBackend:
    streaming = False

    def __init__(self, device: GPUDevice, manager: NvmlManager) -> None:
        self._device = device
        self._manager = manager

    def collect(self) -> tuple[MetricSample, ...]:
        metrics = self._manager.collect_for_slot(self._device.pci_slot)
        return tuple(
            device_metric(self._device, metric_name, metric_value)
            for metric_name, metric_value in sorted(metrics.items())
        )

    def close(self) -> None:
        return None


def normalize_pci_slot(value: str) -> str:
    parts = value.strip().lower().split(":")
    if len(parts) != 3 or "." not in parts[2]:
        raise ValueError(f"invalid PCI slot {value!r}")
    domain = int(parts[0], 16)
    bus = int(parts[1], 16)
    device_part, function_part = parts[2].split(".", 1)
    device_number = int(device_part, 16)
    function_number = int(function_part, 16)
    return f"{domain:04x}:{bus:02x}:{device_number:02x}.{function_number:x}"


def _select_amd_hwmon(sysfs_path: Path) -> Path | None:
    hwmon_root = sysfs_path / "hwmon"
    if not hwmon_root.exists():
        return None

    children = sorted(path for path in hwmon_root.iterdir() if path.is_dir())
    for child in children:
        if _read_text(child / "name") == "amdgpu":
            return child
    return children[0] if children else None


def _find_hwmon_input(sysfs_path: Path, filename: str) -> Path | None:
    hwmon_root = sysfs_path / "hwmon"
    if not hwmon_root.exists():
        return None
    for hwmon_dir in sorted(path for path in hwmon_root.iterdir() if path.is_dir()):
        candidate = hwmon_dir / filename
        if candidate.exists():
            return candidate
    return None


def _read_scaled_value(path: Path | None, scale: float) -> float | None:
    if path is None:
        return None
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return float(raw) / scale
    except ValueError:
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _nested_float(payload: dict[str, object], parent: str, child: str) -> float | None:
    nested = payload.get(parent)
    if not isinstance(nested, dict):
        return None
    return _coerce_float(nested.get(child))

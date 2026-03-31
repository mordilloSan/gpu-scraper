from __future__ import annotations

import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path

from gpu_scraper.backends import (
    AmdSysfsBackend,
    IncrementalJsonArrayParser,
    IntelGpuTopBackend,
    NvidiaBackend,
    SubprocessIntelSession,
)
from gpu_scraper.models import GPUDevice


class ParserTests(unittest.TestCase):
    def test_incremental_json_parser_handles_stream_chunks(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "intel" / "stream.json"
        data = fixture.read_text(encoding="utf-8")
        parser = IncrementalJsonArrayParser()

        objects = []
        for chunk in (data[:30], data[30:80], data[80:150], data[150:]):
            objects.extend(parser.feed(chunk))

        parser.finish()
        self.assertEqual(len(objects), 2)
        self.assertEqual(objects[0]["frequency"]["actual"], 1200.0)
        self.assertEqual(objects[1]["engines"]["Video"]["busy"], 12.5)


class FakeIntelSession:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = deque(chunks)
        self.closed = False

    def read_chunk(self, _timeout: float) -> bytes:
        if not self._chunks:
            raise EOFError("fake session reached EOF")
        return self._chunks.popleft()

    def close(self, timeout: float = 2.0) -> None:
        del timeout
        self.closed = True


class IntelBackendTests(unittest.TestCase):
    def test_backend_restarts_after_eof(self) -> None:
        device = make_device("intel")
        sessions = deque(
            [
                FakeIntelSession([b"[", b'{"frequency":{"actual":1000.0}']),
                FakeIntelSession(
                    [
                        b'[{"frequency":{"actual":850.0,"requested":900.0},'
                        b'"engines":{"Render/3D":{"busy":50.0}},'
                        b'"imc-bandwidth":{"reads":512.0,"writes":256.0}}]'
                    ]
                ),
            ]
        )

        def factory() -> FakeIntelSession:
            return sessions.popleft()

        backend = IntelGpuTopBackend(
            device, sample_interval=0.1, session_factory=factory
        )

        samples = backend.collect()

        values = {sample.name: sample.value for sample in samples}
        self.assertEqual(values["gpu_core_clock_megahertz"], 850.0)
        self.assertEqual(values["gpu_intel_requested_clock_megahertz"], 900.0)
        self.assertEqual(
            values["gpu_intel_imc_read_bandwidth_mebibytes_per_second"], 512.0
        )
        self.assertEqual(
            values["gpu_intel_imc_write_bandwidth_mebibytes_per_second"], 256.0
        )
        self.assertEqual(len(sessions), 0)

    def test_subprocess_session_kills_process_group(self) -> None:
        session = SubprocessIntelSession(
            [
                sys.executable,
                "-c",
                "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)",
            ]
        )

        session.close(timeout=0.1)

        self.assertIsNotNone(session.poll())


class AmdBackendTests(unittest.TestCase):
    def test_reads_only_card_local_hwmon_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sysfs_path = Path(tmpdir) / "0000:03:00.0"
            hwmon_dir = sysfs_path / "hwmon" / "hwmon7"
            hwmon_dir.mkdir(parents=True)
            (hwmon_dir / "name").write_text("amdgpu", encoding="utf-8")
            (hwmon_dir / "temp1_input").write_text("65500", encoding="utf-8")
            (hwmon_dir / "power1_average").write_text("150000000", encoding="utf-8")
            (hwmon_dir / "freq1_input").write_text("2100000000", encoding="utf-8")
            (hwmon_dir / "freq2_input").write_text("1750000000", encoding="utf-8")
            (sysfs_path / "gpu_busy_percent").write_text("80", encoding="utf-8")
            (sysfs_path / "mem_info_vram_used").write_text(
                "4294967296", encoding="utf-8"
            )
            (sysfs_path / "mem_info_vram_total").write_text(
                "8589934592", encoding="utf-8"
            )
            (hwmon_dir / "fan1_input").write_text("1200", encoding="utf-8")

            backend = AmdSysfsBackend(make_device("amd", sysfs_path=sysfs_path))
            samples = {sample.name: sample.value for sample in backend.collect()}

            self.assertEqual(samples["gpu_temperature_celsius"], 65.5)
            self.assertEqual(samples["gpu_power_watts"], 150.0)
            self.assertEqual(samples["gpu_core_clock_megahertz"], 2100.0)
            self.assertEqual(samples["gpu_memory_clock_megahertz"], 1750.0)
            self.assertEqual(samples["gpu_utilization_ratio"], 0.8)
            self.assertEqual(samples["gpu_memory_used_bytes"], 4294967296.0)
            self.assertEqual(samples["gpu_memory_total_bytes"], 8589934592.0)
            self.assertEqual(samples["gpu_fan_speed_rpm"], 1200.0)


class FakeNvmlManager:
    def collect_for_slot(self, pci_slot: str) -> dict[str, float]:
        if pci_slot != "0000:04:00.0":
            raise RuntimeError("unexpected slot")
        return {
            "gpu_temperature_celsius": 72.0,
            "gpu_power_watts": 215.0,
            "gpu_core_clock_megahertz": 1980.0,
            "gpu_memory_clock_megahertz": 9500.0,
            "gpu_utilization_ratio": 0.97,
            "gpu_memory_utilization_ratio": 0.45,
            "gpu_memory_used_bytes": 4294967296.0,
            "gpu_memory_total_bytes": 8589934592.0,
            "gpu_fan_speed_ratio": 0.55,
            "gpu_nvidia_encoder_utilization_ratio": 0.30,
            "gpu_nvidia_decoder_utilization_ratio": 0.15,
        }


class NvidiaBackendTests(unittest.TestCase):
    def test_collects_metrics_from_manager(self) -> None:
        backend = NvidiaBackend(
            make_device("nvidia", slot="0000:04:00.0"), FakeNvmlManager()
        )

        samples = {sample.name: sample.value for sample in backend.collect()}

        self.assertEqual(samples["gpu_temperature_celsius"], 72.0)
        self.assertEqual(samples["gpu_memory_clock_megahertz"], 9500.0)
        self.assertEqual(samples["gpu_utilization_ratio"], 0.97)
        self.assertEqual(samples["gpu_memory_utilization_ratio"], 0.45)
        self.assertEqual(samples["gpu_memory_used_bytes"], 4294967296.0)
        self.assertEqual(samples["gpu_memory_total_bytes"], 8589934592.0)
        self.assertEqual(samples["gpu_fan_speed_ratio"], 0.55)
        self.assertEqual(samples["gpu_nvidia_encoder_utilization_ratio"], 0.30)
        self.assertEqual(samples["gpu_nvidia_decoder_utilization_ratio"], 0.15)


def make_device(
    vendor: str, sysfs_path: Path | None = None, slot: str = "0000:00:02.0"
) -> GPUDevice:
    sysfs = sysfs_path or Path(f"/sys/devices/pci0000:00/{slot}")
    return GPUDevice(
        card="0",
        card_name="card0",
        card_path=Path("/sys/class/drm/card0"),
        sysfs_path=sysfs,
        vendor=vendor,
        vendor_id={
            "intel": "0x8086",
            "amd": "0x1002",
            "nvidia": "0x10de",
        }[vendor],
        device_id="0x1234",
        pci_slot=slot,
        driver={
            "intel": "i915",
            "amd": "amdgpu",
            "nvidia": "nvidia",
        }[vendor],
    )


if __name__ == "__main__":
    unittest.main()

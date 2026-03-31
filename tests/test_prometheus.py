from __future__ import annotations

import unittest
from pathlib import Path

from gpu_scraper.models import GPUDevice, MetricSample, StateSnapshot
from gpu_scraper.prometheus import render_metrics


class PrometheusRenderingTests(unittest.TestCase):
    def test_render_metrics_sorts_labels_and_escapes_values(self) -> None:
        device = GPUDevice(
            card="0",
            card_name="card0",
            card_path=Path("/sys/class/drm/card0"),
            sysfs_path=Path("/sys/devices/pci0000:00/0000:00:02.0"),
            vendor="intel",
            vendor_id="0x8086",
            device_id="0x46d1",
            pci_slot="0000:00:02.0",
            driver="i915",
        )
        snapshot = StateSnapshot(
            device=device,
            up=1.0,
            last_success_timestamp=1234.5,
            collection_errors=2,
            samples=(
                MetricSample(
                    name="gpu_intel_engine_utilization_ratio",
                    value=0.75,
                    labels=(
                        ("vendor", "intel"),
                        ("engine", 'Render/3D "main"\n'),
                        ("pci_slot", "0000:00:02.0"),
                        ("card", "0"),
                    ),
                ),
            ),
        )

        payload = render_metrics("0.1.0", [snapshot]).decode("utf-8")

        self.assertIn('gpu_scraper_build_info{version="0.1.0"} 1', payload)
        gpu_info_line = (
            'gpu_info{card="0",device_id="0x46d1",'
            'driver="i915",pci_slot="0000:00:02.0",'
            'vendor="intel"} 1'
        )
        self.assertIn(gpu_info_line, payload)
        self.assertIn('engine="Render/3D \\"main\\"\\n"', payload)
        errors_line = (
            "gpu_scraper_collection_errors_total"
            '{card="0",pci_slot="0000:00:02.0",'
            'vendor="intel"} 2'
        )
        self.assertIn(errors_line, payload)


if __name__ == "__main__":
    unittest.main()

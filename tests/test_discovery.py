from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from gpu_scraper.discovery import discover_gpus


class DiscoveryTests(unittest.TestCase):
    def test_discovers_supported_cards_and_ignores_connectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            drm_root = root / "drm"
            drm_root.mkdir()
            self._make_card(
                root, drm_root, "card0", "0000:00:02.0", "0x8086", "0x46d1", "i915"
            )
            self._make_card(
                root, drm_root, "card1", "0000:03:00.0", "0x1002", "0x73bf", "amdgpu"
            )
            (drm_root / "card0-DP-1").mkdir()
            self._make_card(
                root, drm_root, "card2", "0000:04:00.0", "0x1234", "0xbeef", "unknown"
            )

            devices = discover_gpus(drm_root)

            self.assertEqual([device.card for device in devices], ["0", "1"])
            self.assertEqual([device.vendor for device in devices], ["intel", "amd"])
            self.assertEqual(devices[0].pci_slot, "0000:00:02.0")
            self.assertEqual(devices[1].driver, "amdgpu")

    def _make_card(
        self,
        root: Path,
        drm_root: Path,
        card_name: str,
        slot: str,
        vendor_id: str,
        device_id: str,
        driver: str,
    ) -> None:
        card_dir = drm_root / card_name
        card_dir.mkdir()

        sysfs_path = root / "devices" / slot
        sysfs_path.mkdir(parents=True)
        (sysfs_path / "vendor").write_text(vendor_id, encoding="utf-8")
        (sysfs_path / "device").write_text(device_id, encoding="utf-8")

        driver_target = root / "drivers" / driver
        driver_target.mkdir(parents=True, exist_ok=True)
        os.symlink(driver_target, sysfs_path / "driver")
        os.symlink(sysfs_path, card_dir / "device")


if __name__ == "__main__":
    unittest.main()

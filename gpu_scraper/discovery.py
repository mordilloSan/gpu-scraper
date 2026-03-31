from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from gpu_scraper.models import GPUDevice

LOGGER = logging.getLogger(__name__)

SUPPORTED_VENDORS = {
    "0x1002": "amd",
    "0x10de": "nvidia",
    "0x8086": "intel",
}
CARD_PATTERN = re.compile(r"^card(\d+)$")


def discover_gpus(drm_root: Path = Path("/sys/class/drm")) -> list[GPUDevice]:
    devices: list[GPUDevice] = []
    if not drm_root.exists():
        LOGGER.warning("DRM root %s does not exist", drm_root)
        return devices

    for card_path in sorted(drm_root.iterdir(), key=lambda path: path.name):
        match = CARD_PATTERN.fullmatch(card_path.name)
        if match is None:
            continue
        gpu = _build_device(card_path, match.group(1))
        if gpu is None:
            continue
        LOGGER.info(
            "Discovered %s GPU: card%s pci=%s device=%s driver=%s",
            gpu.vendor,
            gpu.card,
            gpu.pci_slot,
            gpu.device_id,
            gpu.driver,
        )
        devices.append(gpu)

    if devices:
        vendors = sorted({d.vendor for d in devices})
        LOGGER.info(
            "Discovery complete: %d GPU(s), vendor(s): %s",
            len(devices),
            ", ".join(vendors),
        )
    else:
        LOGGER.warning("Discovery complete: no supported GPUs found")

    return devices


def _build_device(card_path: Path, card_index: str) -> GPUDevice | None:
    device_link = card_path / "device"
    if not device_link.exists():
        return None

    sysfs_path = device_link.resolve()
    vendor_id = _read_text(sysfs_path / "vendor")
    if vendor_id is None:
        return None
    vendor_id = vendor_id.lower()
    vendor = SUPPORTED_VENDORS.get(vendor_id)
    if vendor is None:
        LOGGER.info("Skipping unsupported GPU vendor %s at %s", vendor_id, card_path)
        return None

    device_id = _read_text(sysfs_path / "device") or "unknown"
    driver = _read_driver_name(sysfs_path)
    pci_slot = sysfs_path.name.lower()

    return GPUDevice(
        card=card_index,
        card_name=card_path.name,
        card_path=card_path,
        sysfs_path=sysfs_path,
        vendor=vendor,
        vendor_id=vendor_id,
        device_id=device_id.lower(),
        pci_slot=pci_slot,
        driver=driver,
    )


def _read_driver_name(sysfs_path: Path) -> str:
    driver_link = sysfs_path / "driver"
    if not driver_link.exists():
        return "unknown"
    try:
        return os.path.basename(os.path.realpath(driver_link))
    except OSError:
        return "unknown"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

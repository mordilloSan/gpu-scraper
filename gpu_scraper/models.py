from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock


@dataclass(frozen=True)
class GPUDevice:
    card: str
    card_name: str
    card_path: Path
    sysfs_path: Path
    vendor: str
    vendor_id: str
    device_id: str
    pci_slot: str
    driver: str

    @property
    def base_labels(self) -> dict[str, str]:
        return {
            "card": self.card,
            "pci_slot": self.pci_slot,
            "vendor": self.vendor,
        }

    def sort_key(self) -> tuple[int, str, str]:
        return (int(self.card), self.vendor, self.pci_slot)


@dataclass(frozen=True)
class MetricSample:
    name: str
    value: float
    labels: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class StateSnapshot:
    device: GPUDevice
    up: float
    last_success_timestamp: float
    collection_errors: int
    samples: tuple[MetricSample, ...]


class CollectorState:
    def __init__(self, device: GPUDevice) -> None:
        self._device = device
        self._lock = Lock()
        self._up = 0.0
        self._last_success_timestamp = 0.0
        self._collection_errors = 0
        self._samples: tuple[MetricSample, ...] = ()

    @property
    def device(self) -> GPUDevice:
        return self._device

    def record_success(
        self,
        samples: tuple[MetricSample, ...],
        timestamp: float,
    ) -> None:
        with self._lock:
            self._up = 1.0
            self._last_success_timestamp = timestamp
            self._samples = samples

    def record_failure(self) -> None:
        with self._lock:
            self._up = 0.0
            self._collection_errors += 1
            self._samples = ()

    def snapshot(self) -> StateSnapshot:
        with self._lock:
            return StateSnapshot(
                device=self._device,
                up=self._up,
                last_success_timestamp=self._last_success_timestamp,
                collection_errors=self._collection_errors,
                samples=self._samples,
            )

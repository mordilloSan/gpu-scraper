from __future__ import annotations

import signal
import threading
import time
import unittest
import urllib.request
from unittest import mock

from gpu_scraper.models import CollectorState
from gpu_scraper.prometheus import device_metric
from gpu_scraper.service import CollectorWorker, ExporterService
from tests.test_backends import make_device


class BlockingBackend:
    streaming = False

    def __init__(self, device_card: str) -> None:
        self.calls = 0
        self.closed = False
        self._device_card = device_card
        self._release = threading.Event()

    def collect(self):
        self.calls += 1
        if self.calls == 1:
            device = make_device("amd")
            return (device_metric(device, "gpu_temperature_celsius", 55.0),)
        self._release.wait(timeout=10.0)
        return ()

    def close(self) -> None:
        self.closed = True
        self._release.set()


class ServiceTests(unittest.TestCase):
    def test_http_endpoint_uses_cached_metrics(self) -> None:
        device = make_device("amd")
        backend = BlockingBackend(device.card)
        stop_event = threading.Event()
        state = CollectorState(device)
        worker = CollectorWorker(
            backend, state, sample_interval=0.01, stop_event=stop_event
        )
        service = ExporterService("127.0.0.1", 0, [worker], [])

        service.start()
        try:
            self._wait_for(lambda: state.snapshot().up == 1.0)
            time.sleep(0.05)
            start = time.monotonic()
            with urllib.request.urlopen(
                f"http://127.0.0.1:{service.bound_port}/metrics", timeout=1.0
            ) as response:
                body = response.read().decode("utf-8")
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 0.5)
            self.assertIn("gpu_temperature_celsius", body)
        finally:
            service.stop()

    def test_shutdown_closes_backend_and_stops_worker(self) -> None:
        device = make_device("amd")
        backend = BlockingBackend(device.card)
        stop_event = threading.Event()
        state = CollectorState(device)
        worker = CollectorWorker(
            backend, state, sample_interval=0.01, stop_event=stop_event
        )
        service = ExporterService("127.0.0.1", 0, [worker], [])

        service.start()
        self._wait_for(lambda: state.snapshot().up == 1.0)

        service.request_shutdown()
        service.stop()

        self.assertTrue(backend.closed)
        self.assertFalse(worker.is_alive())

    def test_signal_handlers_request_shutdown(self) -> None:
        device = make_device("amd")
        backend = BlockingBackend(device.card)
        stop_event = threading.Event()
        state = CollectorState(device)
        worker = CollectorWorker(
            backend, state, sample_interval=0.01, stop_event=stop_event
        )
        service = ExporterService("127.0.0.1", 0, [worker], [])
        installed: dict[int, object] = {}

        with mock.patch(
            "signal.signal",
            side_effect=lambda sig, handler: installed.__setitem__(sig, handler),
        ):
            service.install_signal_handlers()

        self.assertIn(signal.SIGTERM, installed)
        handler = installed[signal.SIGTERM]
        handler(signal.SIGTERM, None)

        self.assertTrue(service.stop_event.is_set())
        service.stop()

    def _wait_for(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition was not met before timeout")


if __name__ == "__main__":
    unittest.main()

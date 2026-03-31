from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gpu_scraper import __version__
from gpu_scraper.backends import (
    AmdSysfsBackend,
    CollectorBackend,
    IntelGpuTopBackend,
    NvidiaBackend,
    NvmlManager,
)
from gpu_scraper.discovery import discover_gpus
from gpu_scraper.models import CollectorState, GPUDevice
from gpu_scraper.prometheus import render_metrics

LOGGER = logging.getLogger(__name__)


class CollectorWorker(threading.Thread):
    def __init__(
        self,
        backend: CollectorBackend,
        state: CollectorState,
        sample_interval: float,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=f"collector-{state.device.card_name}", daemon=True)
        self._backend = backend
        self._state = state
        self._sample_interval = sample_interval
        self._stop_event = stop_event
        self._failure_backoff = max(0.25, min(sample_interval, 2.0))

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                samples = self._backend.collect()
            except Exception as exc:  # pragma: no cover - exercised via service tests
                LOGGER.warning(
                    "Collector for %s failed: %s",
                    self._state.device.card_name,
                    exc,
                )
                self._state.record_failure()
                if self._stop_event.wait(self._failure_backoff):
                    break
            else:
                self._state.record_success(samples, time.time())
                if not self._backend.streaming and self._stop_event.wait(
                    self._sample_interval
                ):
                    break

    def close(self) -> None:
        self._backend.close()


class MetricsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, server_address: tuple[str, int], service: ExporterService
    ) -> None:
        self.service = service
        super().__init__(server_address, MetricsHandler)


class MetricsHandler(BaseHTTPRequestHandler):
    server_version = f"gpu-scraper/{__version__}"

    def do_GET(self) -> None:
        if self.path != "/metrics":
            self.send_error(HTTPStatus.NOT_FOUND, "only /metrics is supported")
            return

        payload = self.server.service.render_metrics()  # type: ignore[attr-defined]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.debug("HTTP %s - %s", self.address_string(), format % args)


@dataclass(frozen=True)
class RuntimeOptions:
    host: str
    port: int
    sample_interval: float
    intel_gpu_top_bin: str
    nvml_lib: str | None


class ExporterService:
    def __init__(
        self,
        host: str,
        port: int,
        workers: list[CollectorWorker],
        shared_resources: Iterable[object],
    ) -> None:
        self._host = host
        self._port = port
        self._workers = workers
        self._shared_resources = list(shared_resources)
        self._stop_event = threading.Event()
        for worker in self._workers:
            worker._stop_event = self._stop_event
        self._server: MetricsHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._shutdown_lock = threading.Lock()

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    @property
    def states(self) -> list[CollectorState]:
        return [worker._state for worker in self._workers]

    def start(self) -> None:
        for worker in self._workers:
            worker.start()
        self._server = MetricsHTTPServer((self._host, self._port), self)
        self._server_thread = threading.Thread(
            target=self._serve_http,
            name="http-server",
            daemon=True,
        )
        self._server_thread.start()

    def wait(self) -> None:
        self._stop_event.wait()
        self.stop()

    def install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: object) -> None:
            LOGGER.info("Received signal %s, shutting down", signum)
            self.request_shutdown()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def request_shutdown(self) -> None:
        with self._shutdown_lock:
            if self._stop_event.is_set():
                return
            self._stop_event.set()
            if self._server is not None:
                threading.Thread(
                    target=self._server.shutdown,
                    name="http-shutdown",
                    daemon=True,
                ).start()

    def stop(self) -> None:
        self.request_shutdown()
        if self._server_thread is not None and self._server_thread.is_alive():
            self._server_thread.join(timeout=3.0)
        for worker in self._workers:
            worker.close()
        for worker in self._workers:
            if worker.ident is not None:
                worker.join(timeout=3.0)
        for resource in self._shared_resources:
            close = getattr(resource, "close", None)
            if callable(close):
                close()
        if self._server is not None:
            self._server.server_close()

    def render_metrics(self) -> bytes:
        return render_metrics(__version__, [state.snapshot() for state in self.states])

    def _serve_http(self) -> None:
        assert self._server is not None
        self._server.serve_forever(poll_interval=0.5)


def create_service(options: RuntimeOptions) -> ExporterService:
    devices = discover_gpus()
    backends: list[CollectorBackend] = []
    states: list[CollectorState] = []
    shared_resources: list[object] = []

    nvml_manager: NvmlManager | None = None
    if any(device.vendor == "nvidia" for device in devices):
        nvml_manager = NvmlManager(options.nvml_lib)
        shared_resources.append(nvml_manager)
        if nvml_manager.error:
            LOGGER.warning("NVML unavailable: %s", nvml_manager.error)

    for device in devices:
        backend = _build_backend(
            device, options.sample_interval, options.intel_gpu_top_bin, nvml_manager
        )
        backends.append(backend)
        states.append(CollectorState(device))

    workers = [
        CollectorWorker(
            backend=backend,
            state=state,
            sample_interval=options.sample_interval,
            stop_event=threading.Event(),
        )
        for backend, state in zip(backends, states, strict=True)
    ]
    return ExporterService(
        host=options.host,
        port=options.port,
        workers=workers,
        shared_resources=shared_resources,
    )


def _build_backend(
    device: GPUDevice,
    sample_interval: float,
    intel_gpu_top_bin: str,
    nvml_manager: NvmlManager | None,
) -> CollectorBackend:
    if device.vendor == "intel":
        return IntelGpuTopBackend(
            device, sample_interval, intel_gpu_top_bin=intel_gpu_top_bin
        )
    if device.vendor == "amd":
        return AmdSysfsBackend(device)
    if device.vendor == "nvidia":
        if nvml_manager is None:
            raise RuntimeError("NVIDIA device discovered without NVML manager")
        return NvidiaBackend(device, nvml_manager)
    raise RuntimeError(f"unsupported vendor {device.vendor!r}")

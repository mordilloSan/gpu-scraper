from __future__ import annotations

import argparse
import logging

from gpu_scraper.service import RuntimeOptions, create_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prometheus GPU metrics exporter")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP listen address")
    parser.add_argument("--port", type=int, default=10043, help="HTTP listen port")
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=1.0,
        help="sample interval in seconds",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging verbosity",
    )
    parser.add_argument(
        "--intel-gpu-top-bin",
        default="intel_gpu_top",
        help="path to the intel_gpu_top binary",
    )
    parser.add_argument(
        "--nvml-lib",
        default=None,
        help="path to libnvidia-ml.so.1 if it is not discoverable by the loader",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    options = RuntimeOptions(
        host=args.host,
        port=args.port,
        sample_interval=args.sample_interval,
        intel_gpu_top_bin=args.intel_gpu_top_bin,
        nvml_lib=args.nvml_lib,
    )
    service = create_service(options)
    try:
        service.start()
        service.install_signal_handlers()
        service.wait()
    except Exception:
        logging.getLogger(__name__).exception("gpu-scraper failed")
        service.stop()
        return 1
    return 0

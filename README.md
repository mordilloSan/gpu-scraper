# GPU Scraper

`gpu-scraper` is a single-daemon Prometheus exporter for Linux GPU metrics. It discovers DRM cards at startup, collects vendor-specific metrics, and serves them on one `/metrics` endpoint.

## Features

- Intel collection via `intel_gpu_top -J`
- AMD collection via card-local sysfs `hwmon` files
- NVIDIA collection via NVML loaded with `ctypes`
- No third-party runtime dependencies
- One background collector thread per discovered GPU

## Running

```bash
python3 -m gpu_scraper --host 0.0.0.0 --port 10043
```

Or install it as a console script:

```bash
python3 -m pip install .
gpu-scraper --host 0.0.0.0 --port 10043
```

## Requirements

- Linux with `/sys/class/drm`
- Read access to `/sys`
- Intel: access to the relevant `/dev/dri` device nodes and host perf permissions. Non-root use depends on system perf policy and may require `CAP_PERFMON` plus a suitable `perf_event_paranoid` setting.
- NVIDIA: the NVML shared library must be available to the dynamic loader, or passed explicitly with `--nvml-lib /absolute/path/to/libnvidia-ml.so.1`

## Known Limitations

- GPU discovery is DRM-first. NVIDIA devices visible to NVML but lacking DRM card nodes are not exported in v1.
- GPU hot-plug is not supported. Restart the daemon after hardware changes.
- Metrics are emitted only when the backend exposes them directly. Missing per-vendor sensors are omitted.

## Development

Run the test suite with:

```bash
python3 -m unittest discover -s tests -v
```

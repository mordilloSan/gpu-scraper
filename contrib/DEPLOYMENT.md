# Deployment Notes

## Prerequisites

- Linux with `/sys/class/drm`
- Read access to `/sys`
- Intel: access to the relevant `/dev/dri` nodes and host perf permissions. Non-root use depends on system perf policy and may require `CAP_PERFMON` plus an appropriate `perf_event_paranoid` value.
- NVIDIA: NVML must be available to the loader or passed explicitly with `--nvml-lib /absolute/path/to/libnvidia-ml.so.1`

## Runtime

- Default endpoint: `http://0.0.0.0:10043/metrics`
- Startup-only device discovery. Restart the service after GPU topology changes.
- Graceful shutdown on `SIGTERM` and `SIGINT` stops the HTTP server, stops collector threads, and tears down `intel_gpu_top` subprocess groups.

## Known Limitations

- DRM-first discovery means NVML-only NVIDIA devices without DRM card nodes are not exported in v1.
- Missing per-vendor sensors are omitted instead of synthesized.

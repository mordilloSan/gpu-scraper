from __future__ import annotations

import math
from collections.abc import Iterable

from gpu_scraper.models import GPUDevice, MetricSample, StateSnapshot


def metric_sample(
    name: str,
    value: float,
    labels: dict[str, str] | None = None,
) -> MetricSample:
    return MetricSample(
        name=name,
        value=float(value),
        labels=_sorted_labels(labels or {}),
    )


def device_metric(
    device: GPUDevice,
    name: str,
    value: float,
    extra_labels: dict[str, str] | None = None,
) -> MetricSample:
    labels = dict(device.base_labels)
    if extra_labels:
        labels.update(extra_labels)
    return metric_sample(name, value, labels)


def render_metrics(version: str, snapshots: Iterable[StateSnapshot]) -> bytes:
    samples: list[MetricSample] = [
        metric_sample("gpu_scraper_build_info", 1.0, {"version": version})
    ]
    for snapshot in sorted(snapshots, key=lambda item: item.device.sort_key()):
        device = snapshot.device
        info_labels = dict(device.base_labels)
        info_labels["device_id"] = device.device_id
        info_labels["driver"] = device.driver
        samples.append(metric_sample("gpu_info", 1.0, info_labels))
        samples.append(device_metric(device, "gpu_up", snapshot.up))
        samples.append(
            device_metric(
                device,
                "gpu_last_success_timestamp_seconds",
                snapshot.last_success_timestamp,
            )
        )
        samples.append(
            device_metric(
                device,
                "gpu_scraper_collection_errors_total",
                float(snapshot.collection_errors),
            )
        )
        samples.extend(snapshot.samples)

    lines = [_format_sample(sample) for sample in sorted(samples, key=_sample_sort_key)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _sample_sort_key(sample: MetricSample) -> tuple[str, tuple[tuple[str, str], ...]]:
    return (sample.name, sample.labels)


def _sorted_labels(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


def _format_sample(sample: MetricSample) -> str:
    label_text = ""
    if sample.labels:
        rendered = [
            f'{key}="{_escape_label_value(value)}"' for key, value in sample.labels
        ]
        label_text = "{" + ",".join(rendered) + "}"
    return f"{sample.name}{label_text} {_format_value(sample.value)}"


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value.is_integer():
        return str(int(value))
    return format(value, ".15g")

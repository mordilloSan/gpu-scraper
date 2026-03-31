FROM python:3.12-slim

ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/mordilloSan/gpu-scraper"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.description="Prometheus GPU metrics exporter for Intel, AMD, and NVIDIA GPUs"

RUN apt-get update \
    && apt-get install -y --no-install-recommends intel-gpu-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY gpu_scraper/ gpu_scraper/
COPY pyproject.toml .

RUN pip install --no-cache-dir .

EXPOSE 10043

ENTRYPOINT ["gpu-scraper"]
CMD ["--host", "0.0.0.0", "--port", "10043"]

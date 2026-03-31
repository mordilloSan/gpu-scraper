FROM python:3.12-slim

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

PYTHON   ?= python3
HOST     ?= 0.0.0.0
PORT     ?= 10043
IMAGE    ?= gpu-scraper
REGISTRY ?= ghcr.io/mordillosan/gpu-scraper
VERSION  := $(shell $(PYTHON) -c "from gpu_scraper import __version__; print(__version__)")

.PHONY: help test lint format fix run docker docker-run docker-push version tag

help:
	@printf '%s\n' 'Targets:'
	@printf '  %-14s %s\n' 'test'        'Run the unit test suite'
	@printf '  %-14s %s\n' 'lint'        'Run syntax checks and ruff'
	@printf '  %-14s %s\n' 'format'      'Auto-format with ruff'
	@printf '  %-14s %s\n' 'fix'         'Auto-fix lint issues'
	@printf '  %-14s %s\n' 'run'         'Start the exporter locally'
	@printf '  %-14s %s\n' 'docker'      'Build the Docker image'
	@printf '  %-14s %s\n' 'docker-run'  'Build and run via docker compose'
	@printf '  %-14s %s\n' 'docker-push' 'Build and push to GHCR'
	@printf '  %-14s %s\n' 'version'     'Print current version'
	@printf '  %-14s %s\n' 'tag'         'Create a git release tag'

test:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	$(PYTHON) -m compileall gpu_scraper tests
	ruff check .

format:
	ruff format .

fix:
	ruff check --fix .

run:
	$(PYTHON) -m gpu_scraper --host $(HOST) --port $(PORT)

version:
	@echo $(VERSION)

docker:
	docker build --build-arg VERSION=$(VERSION) -t $(IMAGE):$(VERSION) -t $(IMAGE):latest .

docker-run:
	GPU_SCRAPER_IMAGE=$(IMAGE):latest docker compose up

docker-push: docker
	docker tag $(IMAGE):$(VERSION) $(REGISTRY):$(VERSION)
	docker tag $(IMAGE):latest $(REGISTRY):latest
	docker push $(REGISTRY):$(VERSION)
	docker push $(REGISTRY):latest

tag:
	@if git diff --quiet HEAD; then \
		git tag -a "v$(VERSION)" -m "Release $(VERSION)"; \
		printf 'Tagged v%s. Push with: git push origin v%s\n' "$(VERSION)" "$(VERSION)"; \
	else \
		printf 'ERROR: Working tree is dirty. Commit changes first.\n'; \
		exit 1; \
	fi

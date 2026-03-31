PYTHON   ?= python3
VENV     ?= .venv
TOOL_PYTHON := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,$(PYTHON))
HOST     ?= 0.0.0.0
PORT     ?= 10043
IMAGE    ?= gpu-scraper
REGISTRY ?= ghcr.io/mordillosan/gpu-scraper
VERSION  := $(shell $(PYTHON) -c "from gpu_scraper import __version__; print(__version__)")

.PHONY: help setup-dev ensure-ruff ensure-pyright test lint typecheck format fix full run docker docker-run docker-push version tag

help:
	@printf '%s\n' 'Targets:'
	@printf '  %-14s %s\n' 'setup-dev'   'Create .venv and install ruff/pyright'
	@printf '  %-14s %s\n' 'test'        'Run the unit test suite'
	@printf '  %-14s %s\n' 'lint'        'Run syntax checks and ruff'
	@printf '  %-14s %s\n' 'typecheck'   'Run pyright static type checks'
	@printf '  %-14s %s\n' 'fix'         'Run ruff format and apply lint fixes'
	@printf '  %-14s %s\n' 'full'        'Run fix, lint, typecheck, and test'
	@printf '  %-14s %s\n' 'run'         'Start the exporter locally'
	@printf '  %-14s %s\n' 'docker'      'Build the Docker image'
	@printf '  %-14s %s\n' 'docker-run'  'Build and run via docker compose'
	@printf '  %-14s %s\n' 'docker-push' 'Build and push to GHCR'
	@printf '  %-14s %s\n' 'version'     'Print current version'
	@printf '  %-14s %s\n' 'tag'         'Create a git release tag'

setup-dev:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --upgrade pip
	$(VENV)/bin/python -m pip install ruff pyright

ensure-ruff:
	@if ! $(TOOL_PYTHON) -m ruff --version >/dev/null 2>&1; then \
		printf '%s\n' 'ruff is not installed for $(TOOL_PYTHON). Run `make setup-dev`.'; \
		exit 1; \
	fi

ensure-pyright:
	@if ! $(TOOL_PYTHON) -m pyright --version >/dev/null 2>&1; then \
		printf '%s\n' 'pyright is not installed for $(TOOL_PYTHON). Run `make setup-dev`.'; \
		exit 1; \
	fi

test:
	$(PYTHON) -m unittest discover -s tests -v

lint: ensure-ruff
	$(PYTHON) -m compileall gpu_scraper tests
	$(TOOL_PYTHON) -m ruff check .

typecheck: ensure-pyright
	$(TOOL_PYTHON) -m pyright

fix: ensure-ruff
	$(TOOL_PYTHON) -m ruff format .
	$(TOOL_PYTHON) -m ruff check --fix .

format: fix

full: fix lint typecheck test

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

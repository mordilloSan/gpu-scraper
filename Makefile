PYTHON ?= python3
HOST ?= 0.0.0.0
PORT ?= 10043

.PHONY: help test lint format fix run

help:
	@printf '%s\n' 'Targets:'
	@printf '  %-12s %s\n' 'test' 'Run the unit test suite'
	@printf '  %-12s %s\n' 'lint' 'Run syntax checks and ruff if available'
	@printf '  %-12s %s\n' 'run' 'Start the exporter'

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

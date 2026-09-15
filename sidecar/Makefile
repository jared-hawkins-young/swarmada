# swarmada-sidecar foundation make targets.
# The drift-detection example carries its own Makefile with `proto-gen`
# for its .proto contract.

PYTHON ?= python3.12
UV ?= uv
IMAGE ?= swarmada-sidecar:local

.PHONY: help install lint typecheck format build docker docker-run clean

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "%-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install dev dependencies via uv
	$(UV) pip install -e ".[dev]"

lint: ## Ruff lint + format check
	$(PYTHON) -m ruff check src
	$(PYTHON) -m ruff format --check src

typecheck: ## Mypy strict typecheck
	$(PYTHON) -m mypy src

format: ## Auto-format with ruff
	$(PYTHON) -m ruff format src
	$(PYTHON) -m ruff check --fix src

build: ## Build the wheel
	$(PYTHON) -m build

docker: ## Build the container image
	docker build -t $(IMAGE) .

docker-run: ## Run the container locally (dev only)
	docker run --rm -p 9090:9090 \
	    -e SIDECAR_LANGFUSE_HOST=$(SIDECAR_LANGFUSE_HOST) \
	    -e SIDECAR_LANGFUSE_PUBLIC_KEY=$(SIDECAR_LANGFUSE_PUBLIC_KEY) \
	    -e SIDECAR_LANGFUSE_SECRET_KEY=$(SIDECAR_LANGFUSE_SECRET_KEY) \
	    -e SIDECAR_LANGFUSE_PROMPT_VERSION=$(SIDECAR_LANGFUSE_PROMPT_VERSION) \
	    -e SIDECAR_LITELLM_HOST=$(SIDECAR_LITELLM_HOST) \
	    $(IMAGE)

clean: ## Remove build + cache artifacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete

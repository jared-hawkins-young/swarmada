# drift-detection example make targets.

PYTHON ?= python3.12
UV ?= uv

.PHONY: help install proto-gen proto-lint run lint typecheck format clean

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "%-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install with dev extras and the sibling foundation
	$(UV) pip install -e ".[dev]"

proto-gen: ## Regenerate Python gRPC stubs from proto/
	mkdir -p src/drift_detection/proto_gen
	touch src/drift_detection/proto_gen/__init__.py
	$(PYTHON) -m grpc_tools.protoc \
	    -I proto \
	    --python_out=src/drift_detection/proto_gen \
	    --grpc_python_out=src/drift_detection/proto_gen \
	    --pyi_out=src/drift_detection/proto_gen \
	    proto/sidecar/v1/sidecar.proto
	# Regenerate package markers under the proto tree so imports resolve.
	find src/drift_detection/proto_gen -type d -exec touch {}/__init__.py \;
	@echo "Stubs regenerated under src/drift_detection/proto_gen/sidecar/v1/"

proto-lint: ## Validate proto syntax (best-effort; requires buf or protoc)
	@which buf >/dev/null 2>&1 && buf lint proto || \
	    (which protoc >/dev/null 2>&1 && protoc --proto_path=proto proto/sidecar/v1/sidecar.proto -o /dev/null) || \
	    (echo "neither buf nor protoc found; skipping" && exit 0)

run: ## Run the drift-detection agent locally (assumes env from sidecar/docs/DEV.md is set)
	$(PYTHON) -m drift_detection

lint: ## Ruff lint + format check
	$(PYTHON) -m ruff check src
	$(PYTHON) -m ruff format --check src

typecheck: ## Mypy strict typecheck
	$(PYTHON) -m mypy src

format: ## Auto-format with ruff
	$(PYTHON) -m ruff format src
	$(PYTHON) -m ruff check --fix src

clean: ## Remove build + cache artifacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	rm -rf src/drift_detection/proto_gen
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete

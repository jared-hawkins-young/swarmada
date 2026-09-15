# swarmada-sidecar foundation make targets.
# The drift-detection example carries its own Makefile with `proto-gen`
# for its .proto contract.

PYTHON ?= python3.12
UV ?= uv
IMAGE ?= swarmada-sidecar:local

.PHONY: help install proto-gen proto-lint check-router-only lint typecheck format build docker docker-run clean

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "%-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install dev dependencies via uv
	$(UV) pip install -e ".[dev]"

proto-gen: ## Regenerate the LLMGateway Python stubs
	mkdir -p src/swarmada_sidecar/proto_gen
	touch src/swarmada_sidecar/proto_gen/__init__.py
	$(PYTHON) -m grpc_tools.protoc \
	    -I proto \
	    --python_out=src/swarmada_sidecar/proto_gen \
	    --grpc_python_out=src/swarmada_sidecar/proto_gen \
	    --pyi_out=src/swarmada_sidecar/proto_gen \
	    proto/gateway/v1/gateway.proto
	find src/swarmada_sidecar/proto_gen -type d -exec touch {}/__init__.py \;
	# grpc_tools emits `from gateway.v1 import gateway_pb2` which assumes
	# proto_gen/ is on sys.path. Rewrite to fully-qualified imports so
	# the stubs resolve from the swarmada_sidecar package root.
	find src/swarmada_sidecar/proto_gen -name '*_pb2*.py' -exec \
	    sed -i.bak 's/^from gateway\./from swarmada_sidecar.proto_gen.gateway./' {} \;
	find src/swarmada_sidecar/proto_gen -name '*.bak' -delete
	@echo "Foundation stubs regenerated under src/swarmada_sidecar/proto_gen/gateway/v1/"

proto-lint: ## Validate proto syntax (best-effort; requires buf or protoc)
	@which buf >/dev/null 2>&1 && buf lint proto || \
	    (which protoc >/dev/null 2>&1 && protoc --proto_path=proto proto/gateway/v1/gateway.proto -o /dev/null) || \
	    (echo "neither buf nor protoc found; skipping" && exit 0)

check-router-only: ## Fail if any file in the whole monorepo imports an LLM SDK outside gateway.py
	@echo "Scanning entire monorepo for direct LLM SDK use outside sidecar/src/swarmada_sidecar/gateway.py..."
	@cd $(CURDIR)/.. && \
	    if grep -rnE "^(from|import) +(litellm|openai|anthropic|langchain|google\.generativeai|cohere|mistralai|together)( |\.|$$)" \
	        --include='*.py' --include='*.go' \
	        --exclude-dir=.venv --exclude-dir=venv --exclude-dir=node_modules \
	        --exclude-dir=__pycache__ --exclude-dir=.git \
	        --exclude-dir=proto_gen \
	        . | grep -v 'sidecar/src/swarmada_sidecar/gateway.py'; then \
	        echo ""; \
	        echo "FAIL: direct LLM SDK use detected outside sidecar/src/swarmada_sidecar/gateway.py"; \
	        echo "Route all LLM calls through swarmada_sidecar.gateway.Gateway or the LLMGateway gRPC surface."; \
	        echo "See sidecar/docs/AGENT_PROTOCOL.md."; \
	        exit 1; \
	    else \
	        echo "OK: no direct LLM SDK use outside gateway.py"; \
	    fi

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

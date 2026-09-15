# Multi-stage build for the swarmada-sidecar foundation.
# Release stage uses a Chainguard Python image per Constitution v1.0.0
# (Additional Constraints: Language and Runtime).

# ---- builder ----
FROM python:3.12-slim AS builder

WORKDIR /build

# Install uv for fast, reproducible dependency install
RUN pip install --no-cache-dir uv

COPY pyproject.toml ./
# Compile a locked requirements file (uv sync-friendly)
RUN uv pip compile pyproject.toml --output-file requirements.txt

# Install dependencies to a virtualenv we can copy over
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
RUN uv pip install --python /venv/bin/python -r requirements.txt

# Copy application source
COPY src/ ./src/

# Install the package itself
RUN uv pip install --python /venv/bin/python --no-deps .

# ---- release ----
FROM cgr.dev/chainguard/python:latest AS release

WORKDIR /app

# Copy the virtualenv and the source in
COPY --from=builder /venv /venv
COPY --from=builder /build/src /app/src

ENV PATH="/venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Ports: 9090 Prometheus /metrics + healthchecks.
# The foundation exposes no gRPC surface on its own; downstream images that
# add gRPC services (e.g. the drift-detection example) EXPOSE their own port.
EXPOSE 9090

USER nonroot

ENTRYPOINT ["python", "-m", "swarmada_sidecar.server"]

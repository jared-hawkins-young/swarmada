"""Foundation server entrypoint for the swarmada-sidecar.

Starts an HTTP server exposing `/metrics`, `/healthz`, and `/readyz`, plus
a gRPC server exposing the mandatory `sidecar.gateway.v1.LLMGateway`
service (single Complete RPC — the language-agnostic entry point every
Swarmada component uses to make LLM calls).

Startup runs the fail-closed dependency chain:

1. Load Config; validate active_model; fail on missing required fields.
2. Open SQLite store (a durable path is required even for the foundation
   because the LangGraph primitives may want to persist provenance).
3. Verify Langfuse reachable; fetch pinned prompt (proves registry +
   version).
4. Verify LiteLLM gateway reachable.
5. Register LLMGatewayServicer + start gRPC on `SIDECAR_GRPC_PORT`.

If any gate fails, the pod does not become Ready (Constitution Principle
II, spec FR-010).

Domain-specific services (e.g. the drift-detection example's
DriftSidecarServicer) get registered on the same gRPC server via
downstream modules — see
`examples/drift-detection/src/drift_detection/__main__.py`.
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import threading
import time
from concurrent import futures
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import grpc
import structlog
from prometheus_client import generate_latest
from prometheus_client.core import REGISTRY

from swarmada_sidecar.config import Config, load_config
from swarmada_sidecar.gateway import Gateway
from swarmada_sidecar.langfuse_client import LangfuseClient
from swarmada_sidecar.metrics import dependency_healthy

log = structlog.get_logger(__name__)


class _HealthState:
    """Ready/live signal shared with the HTTP healthcheck server."""

    def __init__(self) -> None:
        self._ready = False
        self._live = True
        self._lock = threading.Lock()

    def set_ready(self, ready: bool) -> None:
        with self._lock:
            self._ready = ready

    def set_live(self, live: bool) -> None:
        with self._lock:
            self._live = live

    def snapshot(self) -> tuple[bool, bool]:
        with self._lock:
            return self._ready, self._live


class _HttpHandler(BaseHTTPRequestHandler):
    health: _HealthState = _HealthState()  # class-level; server thread rebinds

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence default noisy access log; structlog handles our lines.
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            body = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/healthz":
            _, live = _HttpHandler.health.snapshot()
            self.send_response(200 if live else 500)
            self.end_headers()
            self.wfile.write(b"ok" if live else b"not-live")
            return
        if self.path == "/readyz":
            ready, _ = _HttpHandler.health.snapshot()
            self.send_response(200 if ready else 503)
            self.end_headers()
            self.wfile.write(b"ready" if ready else b"not-ready")
            return
        self.send_response(404)
        self.end_headers()


def _start_http_server(port: int, health: _HealthState) -> HTTPServer:
    _HttpHandler.health = health
    server = HTTPServer(("0.0.0.0", port), _HttpHandler)  # noqa: S104
    t = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    t.start()
    return server


def _configure_logging(cfg: Config) -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.log_level),
        format="%(message)s",
        stream=sys.stdout,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, cfg.log_level)
        ),
        cache_logger_on_first_use=True,
    )


def _ensure_sqlite_available(cfg: Config) -> None:
    """Best-effort SQLite reachability probe. The foundation does not own a
    schema — downstream projects create their own tables. This just verifies
    the path is writable so downstream startup won't fail cryptically."""
    cfg.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.sqlite_path)
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


def _startup(cfg: Config) -> tuple[LangfuseClient, Gateway]:
    """Fail-closed startup for the foundation dependencies. Returns
    dependency handles on success; raises on any failure.

    Downstream modules should call this, then build any domain-specific
    resources (stores, aggregators, graphs) on top of the returned
    handles.
    """
    log.info("startup.config_loaded", active_model=cfg.active_model)

    # SQLite path probe (downstream owns schema)
    _ensure_sqlite_available(cfg)
    dependency_healthy.labels(dependency="sqlite").set(1.0)

    # Langfuse
    langfuse = LangfuseClient(cfg)
    langfuse.health_check()
    _ = langfuse.get_prompt()  # verify registry entry present
    dependency_healthy.labels(dependency="langfuse").set(1.0)

    # LiteLLM gateway
    gateway = Gateway(cfg)
    gateway.health_check()
    dependency_healthy.labels(dependency="litellm").set(1.0)

    return langfuse, gateway


def build_grpc_server(
    *,
    cfg: Config,
    gateway: Gateway,
    langfuse: LangfuseClient,
    input_guardrail: Any = None,
    max_workers: int = 16,
) -> grpc.Server:
    """Construct a gRPC server with the foundation's LLMGateway registered.

    Downstream modules call this to get a server with the mandatory
    Complete RPC already wired, then register their own domain services
    on it before starting.
    """
    from swarmada_sidecar.llm_gateway_service import LLMGatewayServicer

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    llm_servicer = LLMGatewayServicer(
        cfg=cfg,
        gateway=gateway,
        langfuse=langfuse,
        input_guardrail=input_guardrail,
    )
    llm_servicer.register(server)
    return server


def main() -> None:
    """Foundation entrypoint.

    Starts:
    - HTTP metrics/healthcheck server on `SIDECAR_METRICS_PORT`
    - gRPC server on `SIDECAR_GRPC_PORT` with the mandatory
      `sidecar.gateway.v1.LLMGateway` service registered

    Blocks on signal until shutdown. Domain-specific gRPC services get
    registered via downstream modules that call `build_grpc_server(...)`
    and then add their own services before starting — see
    `examples/drift-detection/src/drift_detection/__main__.py`.
    """
    cfg = load_config()
    _configure_logging(cfg)

    health = _HealthState()
    http_srv = _start_http_server(cfg.metrics_port, health)

    try:
        langfuse, gateway = _startup(cfg)
    except Exception as exc:  # noqa: BLE001
        log.error("startup.failed", error=str(exc))
        # HTTP server stays up so /readyz can report not-ready to k8s.
        # Loop briefly then exit non-zero.
        time.sleep(5)
        http_srv.shutdown()
        raise

    grpc_server = build_grpc_server(cfg=cfg, gateway=gateway, langfuse=langfuse)
    grpc_server.add_insecure_port(f"[::]:{cfg.grpc_port}")
    grpc_server.start()

    health.set_ready(True)
    log.info(
        "server.started",
        grpc_port=cfg.grpc_port,
        metrics_port=cfg.metrics_port,
        registered_services=["sidecar.gateway.v1.LLMGateway"],
    )

    stop = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        log.info("server.shutdown_requested", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not stop.is_set():
        stop.wait(timeout=1.0)

    health.set_ready(False)
    grpc_server.stop(grace=10)
    langfuse.flush()
    http_srv.shutdown()
    log.info("server.stopped")


if __name__ == "__main__":
    main()

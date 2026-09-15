"""Foundation server entrypoint for the swarmada-sidecar.

Starts an HTTP server exposing `/metrics`, `/healthz`, and `/readyz`, then
runs the fail-closed startup sequence:

1. Load Config; validate active_model; fail on missing required fields.
2. Open SQLite store (a durable path is required even for the foundation
   because the LangGraph primitives may want to persist provenance).
3. Verify Langfuse reachable; fetch pinned prompt (proves registry +
   version).
4. Verify LiteLLM gateway reachable.

If any gate fails, the pod does not become Ready (Constitution Principle
II, spec FR-010).

The foundation does NOT register any gRPC services on its own — it has
no domain RPCs to expose. Extend `main()` (or wrap it) in a downstream
module to add domain services. See
`examples/drift-detection/src/drift_detection/__main__.py` for a reference
implementation that composes this foundation with a DriftSidecarServicer.
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

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


def main() -> None:
    """Foundation entrypoint.

    Starts the HTTP metrics / healthcheck server, runs fail-closed startup,
    then blocks on signal until shutdown. The foundation exposes no gRPC
    services on its own — extend `main()` in a downstream module (see
    `examples/drift-detection/src/drift_detection/__main__.py`) to add
    domain-specific gRPC services on top of the returned handles.
    """
    cfg = load_config()
    _configure_logging(cfg)

    health = _HealthState()
    http_srv = _start_http_server(cfg.metrics_port, health)

    try:
        langfuse, _gateway = _startup(cfg)
    except Exception as exc:  # noqa: BLE001
        log.error("startup.failed", error=str(exc))
        # HTTP server stays up so /readyz can report not-ready to k8s.
        # Loop briefly then exit non-zero.
        time.sleep(5)
        http_srv.shutdown()
        raise

    health.set_ready(True)
    log.info(
        "server.started",
        metrics_port=cfg.metrics_port,
        note="foundation only; register gRPC services in a downstream module",
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
    langfuse.flush()
    http_srv.shutdown()
    log.info("server.stopped")


if __name__ == "__main__":
    main()

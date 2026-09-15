"""Entry point for the drift-detection reference agent.

Composes the swarmada_sidecar foundation primitives with the
drift-detection-specific pieces (config, metrics, aggregator, drift
detector, LangGraph nodes, gRPC servicer) into a runnable server.

Startup sequence (fail-closed at every gate):
1. Load foundation Config + DriftConfig; validate active_model.
2. Open DriftStore; run schema migrations.
3. Verify Langfuse reachable; fetch pinned prompt.
4. Verify LiteLLM gateway reachable.
5. Hydrate rolling windows from persisted snapshots.
6. Compile the drift-diagnosis LangGraph.
7. Start Prometheus /metrics + /healthz + /readyz HTTP endpoint.
8. Start gRPC server exposing SidecarService.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from concurrent import futures
from typing import Any

import grpc
import structlog

from drift_detection.aggregator import AggregatorStore
from drift_detection.drift_config import DriftConfig, load_drift_config
from drift_detection.drift_detector import DriftDetector
from drift_detection.drift_metrics import diagnosis_row_count
from drift_detection.graph_nodes import (
    DRIFT_AS_TYPE_MAP,
    DRIFT_NODES,
    DriftDiagnosisPayload,
    DriftNodeContext,
)
from drift_detection.server_ext import DriftSidecarServicer
from drift_detection.store import DriftStore
from swarmada_sidecar.config import Config, load_config
from swarmada_sidecar.gateway import Gateway
from swarmada_sidecar.graph.graph import build_graph, conditional_route
from swarmada_sidecar.guardrails.input_llamaguard import InputGuardrail
from swarmada_sidecar.guardrails.output_guardrails_ai import OutputGuardrail
from swarmada_sidecar.langfuse_client import LangfuseClient
from swarmada_sidecar.metrics import dependency_healthy
from swarmada_sidecar.server import (
    _HealthState,
    _configure_logging,
    _start_http_server,
)

log = structlog.get_logger(__name__)


def _startup(cfg: Config, drift_cfg: DriftConfig) -> tuple[
    DriftStore,
    AggregatorStore,
    DriftDetector,
    Any,
    LangfuseClient,
    Gateway,
]:
    """Fail-closed startup. Returns dependency handles on success."""
    log.info("startup.config_loaded", active_model=cfg.active_model)

    # SQLite store
    store = DriftStore(cfg.sqlite_path)
    diagnosis_row_count.set(store.diagnosis_count())
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

    # Aggregator (hydrate from snapshots)
    aggregator = AggregatorStore(cfg=drift_cfg, store=store)
    aggregator.hydrate_from_snapshots()

    # Drift detector
    drift_detector = DriftDetector(drift_cfg)

    # Guardrails
    input_guardrail = InputGuardrail(cfg=cfg, gateway=gateway)
    output_guardrail = OutputGuardrail(schema=DriftDiagnosisPayload)

    # LangGraph
    ctx = DriftNodeContext(
        cfg=cfg,
        gateway=gateway,
        langfuse=langfuse,
        input_guardrail=input_guardrail,
        output_guardrail=output_guardrail,
        store=store,
    )
    graph_run = build_graph(
        ctx=ctx,
        nodes=DRIFT_NODES,
        entry_point="fetch_window",
        edges=[
            ("fetch_window", "classify_drift"),
            ("classify_drift", "guardrail_input"),
            ("persist", "respond"),
        ],
        conditional_edges=[
            (
                "guardrail_input",
                conditional_route("llm_diagnose"),
                {"llm_diagnose": "llm_diagnose", "respond": "respond"},
            ),
            (
                "llm_diagnose",
                conditional_route("guardrail_output"),
                {"guardrail_output": "guardrail_output", "respond": "respond"},
            ),
            (
                "guardrail_output",
                conditional_route("persist"),
                {"persist": "persist", "respond": "respond"},
            ),
        ],
        terminal_nodes=["respond"],
        as_type_map=DRIFT_AS_TYPE_MAP,
    )

    return store, aggregator, drift_detector, graph_run, langfuse, gateway


def main() -> None:
    cfg = load_config()
    drift_cfg = load_drift_config()
    _configure_logging(cfg)

    health = _HealthState()
    http_srv = _start_http_server(cfg.metrics_port, health)

    try:
        store, aggregator, drift_detector, graph_run, langfuse, _gateway = _startup(
            cfg, drift_cfg
        )
    except Exception as exc:  # noqa: BLE001
        log.error("startup.failed", error=str(exc))
        # HTTP server stays up so /readyz can report not-ready to k8s.
        time.sleep(5)
        http_srv.shutdown()
        raise

    # Import generated stubs; register the service.
    from drift_detection.proto_gen.sidecar.v1 import (  # type: ignore[import-not-found]
        sidecar_pb2_grpc,
    )

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
    servicer = DriftSidecarServicer(
        cfg=cfg,
        drift_cfg=drift_cfg,
        aggregator=aggregator,
        drift_detector=drift_detector,
        graph_run=graph_run,
        store=store,
        langfuse=langfuse,
    )
    sidecar_pb2_grpc.add_SidecarServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{cfg.grpc_port}")
    server.start()

    health.set_ready(True)
    log.info(
        "server.started",
        grpc_port=cfg.grpc_port,
        metrics_port=cfg.metrics_port,
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
    server.stop(grace=10)
    langfuse.flush()
    store.close()
    http_srv.shutdown()
    log.info("server.stopped")


if __name__ == "__main__":
    sys.exit(main() or 0)

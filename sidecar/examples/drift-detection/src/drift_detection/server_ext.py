"""gRPC service handler for the drift-detection example.

Implements the `sidecar.v1.SidecarService` RPC surface with two methods:
- `SubmitTaskCompletion`: aggregate one sample, evaluate drift, and if
  triggered run the LangGraph diagnosis pipeline inside a root Langfuse
  trace.
- `GetDiagnosis`: look up the persisted diagnosis by (robot_id,
  fleet_action_id).

The generated stubs live under `proto_gen/sidecar/v1/`; regenerate them
with `make proto-gen` from the example directory.
"""

from __future__ import annotations

import time
from typing import Any

import grpc
import structlog

from drift_detection.aggregator import AggregatorStore, TaskSample
from drift_detection.drift_config import DriftConfig
from drift_detection.drift_detector import DriftDetector
from drift_detection.drift_metrics import (
    drift_alerts_total,
    submissions_deduped_total,
)
from drift_detection.store import DriftStore
from swarmada_sidecar.config import Config
from swarmada_sidecar.langfuse_client import LangfuseClient
from swarmada_sidecar.graph.state import AgentState
from swarmada_sidecar.metrics import submission_latency_seconds

log = structlog.get_logger(__name__)


class DriftSidecarServicer:
    """gRPC service handler for the drift-detection agent.

    Imports the generated pb2/pb2_grpc modules lazily so that a repo without
    `make proto-gen` can still import this module for tests that don't
    exercise the gRPC surface.
    """

    def __init__(
        self,
        *,
        cfg: Config,
        drift_cfg: DriftConfig,
        aggregator: AggregatorStore,
        drift_detector: DriftDetector,
        graph_run: Any,
        store: DriftStore,
        langfuse: LangfuseClient,
    ) -> None:
        self._cfg = cfg
        self._drift_cfg = drift_cfg
        self._aggregator = aggregator
        self._drift = drift_detector
        self._graph_run = graph_run
        self._store = store
        self._langfuse = langfuse
        # Import generated stubs
        from drift_detection.proto_gen.sidecar.v1 import (  # type: ignore[import-not-found]
            sidecar_pb2,
            sidecar_pb2_grpc,
        )

        self._pb2 = sidecar_pb2
        self._pb2_grpc = sidecar_pb2_grpc

    def SubmitTaskCompletion(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        started = time.perf_counter()
        try:
            # Dedup by fleet_action_id
            if not self._store.mark_seen(request.fleet_action_id):
                submissions_deduped_total.inc()
                return self._pb2.SubmitAck(accepted=False, reject_reason="duplicate")

            sample = TaskSample(
                robot_id=request.robot_id,
                fleet_action_id=request.fleet_action_id,
                model_id=request.model_id,
                model_version=request.model_version,
                confidence=float(request.confidence),
                class_predictions={
                    p.label: float(p.score) for p in request.class_predictions
                },
                captured_at_epoch=request.captured_at.seconds
                + request.captured_at.nanos / 1e9,
                task_type=request.task_type,
            )
            window = self._aggregator.add_sample(sample)
            drift_event = self._drift.evaluate(window)
            if drift_event is not None:
                drift_alerts_total.labels(
                    robot_id=drift_event.window_key[0],
                    model_id=drift_event.window_key[1],
                    task_type=drift_event.window_key[3],
                ).inc()
                # Run the LangGraph diagnosis pipeline inside a root
                # Langfuse trace so each node span nests under one trace
                # in the Langfuse UI (Constitution Principle III). The
                # trace_id is captured and threaded into the state so it
                # lands on the persisted DriftDiagnosis for correlation.
                state = AgentState(
                    payload={
                        "drift_event": {
                            "window_key": list(drift_event.window_key),
                            "reasons": [r.value for r in drift_event.reasons],
                            "window_snapshot": drift_event.window_snapshot,
                            "triggering_fleet_action_id": drift_event.triggering_fleet_action_id,
                            "triggered_at_epoch": drift_event.triggered_at_epoch,
                        }
                    }
                )
                try:
                    with self._langfuse.root_pipeline(
                        name="drift_diagnosis_pipeline",
                        input={
                            "robot_id": drift_event.window_key[0],
                            "model_id": drift_event.window_key[1],
                            "task_type": drift_event.window_key[3],
                            "reasons": [r.value for r in drift_event.reasons],
                            "triggering_fleet_action_id": drift_event.triggering_fleet_action_id,
                        },
                        metadata={
                            "sidecar.active_model": self._cfg.active_model,
                            "sidecar.prompt_version": self._cfg.langfuse_prompt_version,
                        },
                    ) as trace_id:
                        state.langfuse_trace_id = trace_id
                        self._graph_run(state)
                except Exception as exc:  # noqa: BLE001
                    log.error("graph.pipeline_failed", error=str(exc))
                    # Fail-closed: drift is recorded but diagnosis absent.
                    # Client discovers absence via GetDiagnosis NOT_FOUND.

            return self._pb2.SubmitAck(accepted=True, reject_reason="")
        finally:
            submission_latency_seconds.observe(time.perf_counter() - started)

    def GetDiagnosis(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        row = self._store.get_diagnosis(
            robot_id=request.robot_id,
            fleet_action_id=request.fleet_action_id,
        )
        if row is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return self._pb2.DiagnosisResponse(found=False)

        # Populate the DriftDiagnosis message from the persisted payload.
        # Enum mapping via name -> value.
        enum_name = row.get("recommended_action", "INVESTIGATE")
        try:
            action = self._pb2.RecommendedAction.Value(enum_name)
        except ValueError:
            action = self._pb2.RecommendedAction.INVESTIGATE

        diag = self._pb2.DriftDiagnosis(
            robot_id=row.get("robot_id", request.robot_id),
            fleet_action_id=row.get("fleet_action_id", request.fleet_action_id),
            model_id=row.get("model_id", ""),
            model_version=row.get("model_version", ""),
            task_type=row.get("task_type", ""),
            root_cause_hypotheses=list(row.get("root_cause_hypotheses", [])),
            recommended_action=action,
            confidence=float(row.get("confidence", 0.0)),
            human_readable_summary=row.get("human_readable_summary", ""),
            model_used=row.get("model_used", ""),
            prompt_version=row.get("prompt_version", ""),
            langfuse_trace_id=row.get("langfuse_trace_id", ""),
        )
        return self._pb2.DiagnosisResponse(diagnosis=diag, found=True)

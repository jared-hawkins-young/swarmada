# Contracts

## sidecar.v1.proto

The gRPC contract between the Swarmada Go control plane and this sidecar.

Package: `sidecar.v1`
Service: `SidecarService`
RPCs:
- `SubmitTaskCompletion(TaskCompletionResult) -> SubmitAck`
- `GetDiagnosis(GetDiagnosisRequest) -> DiagnosisResponse`

See [`../data-model.md`](../data-model.md) for the entity definitions this proto binds to.

## Regeneration

Python stubs generated via:

```
uv run python -m grpc_tools.protoc \
  -I proto \
  --python_out=src \
  --grpc_python_out=src \
  --pyi_out=src \
  proto/sidecar/v1/sidecar.proto
```

Go stubs (for the Swarmada control plane side) generated via `protoc-gen-go`
and `protoc-gen-go-grpc`. Command lives in the Swarmada upstream Makefile;
the contract file (this one) is authoritative on both sides.

## Versioning

Per Constitution Principle V, breaking changes require a new proto package
version (`sidecar.v2`) alongside continued support for the current version
during a deprecation window. Non-breaking additions (new optional fields, new
RPCs) MAY land within `sidecar.v1`.

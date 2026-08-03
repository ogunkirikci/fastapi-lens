# Compatibility

Version 0.1 targets:

| Component | Supported range |
|---|---|
| Python | 3.11, 3.12, and 3.13 |
| FastAPI | `>=0.121.0,<0.142.0` |
| Pydantic | v2 through the selected FastAPI release |
| SQLAlchemy | 2.x |

FastAPI dependency and serialization instrumentation use internal call
boundaries guarded by version, signature, and behavior checks. Unsupported
FastAPI versions fail adapter installation instead of silently producing
misleading traces.

The CI quality matrix runs every supported Python version. Capability probes
cover representative FastAPI releases across the declared range, including the
latest supported release. Compatibility with a newer FastAPI version is not
implied until the adapter checks and integration suite pass and the dependency
range is deliberately updated.

Streaming response creation is measured as handler work. Stream iteration and
transmission are represented by lifecycle checkpoints rather than being
mislabelled as handler or serialization execution.

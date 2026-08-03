# Configuration reference

`LensConfig` is immutable and validated at construction. Pass it to `Lens`
before the FastAPI application starts:

```python
from fastapi_lens import Lens, LensConfig

lens = Lens(app, config=LensConfig(...))
```

## Runtime and route selection

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `True` | Trace requests that start after attachment. |
| `include_routes` | `("*",)` | POSIX-like patterns eligible for capture. |
| `exclude_routes` | framework and dashboard paths | Patterns discarded before storage; excludes win. |

`*` matches one or more path characters. Route filtering first performs a
conservative raw-path check and then checks the resolved route template. An
empty include tuple disables capture.

The default excluded paths are:

```python
(
    "/docs",
    "/redoc",
    "/openapi.json",
    "/health",
    "/metrics",
    "/__lens__*",
)
```

The configured dashboard path is always added to the effective exclusion set.

## Capture controls

| Field | Default | Meaning |
|---|---:|---|
| `capture_dependencies` | `True` | Capture dependency graph, cache metadata, setup, and cleanup. |
| `capture_sql` | `False` | Permit explicit SQLAlchemy engine registration. |
| `capture_serialization` | `True` | Capture the supported FastAPI serialization boundary. |
| `slow_request_threshold_ms` | `250.0` | Emit the slow-request diagnostic at or above this duration. |
| `slow_dependency_threshold_ms` | `100.0` | Absolute slow-dependency threshold. |

Handler and lifecycle capture are always installed. Disabling an optional
capture type avoids creating its segments while preserving the rest of the
trace.

## Storage and safety limits

| Field | Default | Meaning |
|---|---:|---|
| `max_traces` | `500` | Maximum snapshots retained by the default memory store. |
| `max_api_page_size` | `200` | Maximum dashboard API list page. |
| `max_segments_per_trace` | `1_000` | Segment limit enforced before storage. |
| `max_attribute_length` | `2_000` | Maximum ordinary untrusted text length. |
| `max_error_length` | `8_000` | Maximum error and diagnostic message length. |
| `max_trace_bytes` | `1_000_000` | Maximum versioned JSON size per stored trace. |
| `max_sql_length` | `2_000` | Maximum normalized SQL display text length. |

Limits must be greater than zero. Diagnostic thresholds may be zero but cannot
be negative.

The default `MemoryTraceStore` is process-local. Multi-worker applications have
one independent buffer and route summary per worker.

## Dashboard

| Field | Default | Meaning |
|---|---|---|
| `dashboard_enabled` | `False` | Mount the dashboard. |
| `dashboard_path` | `"/__lens__"` | Mount path within the FastAPI application. |
| `environment` | `None` | Explicit deployment class. |
| `allow_in_production` | `False` | Required staging and production override. |

Allowed environments are `development`, `test`, `staging`, and `production`.
Development and test still require explicit dashboard enablement. Staging and
production require all of:

```python
LensConfig(
    dashboard_enabled=True,
    environment="production",
    allow_in_production=True,
)
```

and at least one authorization dependency passed through
`dashboard_dependencies`.

## Environment variables

Version 0.1 supports `FASTAPI_LENS_ENABLED` only. It is read when `Lens` is
constructed without an explicit `LensConfig`.

Accepted true values are `true`, `yes`, `on`, and `1`; accepted false values are
`false`, `no`, `off`, and `0`, case-insensitively. Invalid values fail fast.

Precedence is:

```text
runtime enable/disable
    > explicit LensConfig
    > FASTAPI_LENS_ENABLED
    > default
```

Full environment-variable coverage is deferred to a later release.

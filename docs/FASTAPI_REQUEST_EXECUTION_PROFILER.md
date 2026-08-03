# FastAPI Request Execution Profiler

## 1. Project Overview

**Working name:** `fastapi-lens`

`fastapi-lens` is a request execution profiler that understands the FastAPI
request lifecycle and shows where an HTTP request spends its time.

The long-term product vision covers:

- Middleware execution
- Request body reading and parsing
- Pydantic validation
- FastAPI dependency resolution
- Endpoint handler execution
- SQLAlchemy queries
- Outbound HTTP requests
- Response serialization
- Background task execution
- Dependency cleanup

This list describes the product vision. It does not mean that every stage is
measured separately in v0.1. Section 5 is the normative source for what v0.1
measures, exposes only as metadata, or defers to later releases.

Core product statement:

> See exactly where every millisecond of a FastAPI request goes.

This statement describes the product direction. v0.1 must expose unattributed
time and must never label unmeasured time as a known root cause.

---

## 2. Problem Statement

Existing observability tools commonly provide:

- Total endpoint duration
- General-purpose Python call-stack profiles
- Prometheus metrics
- OpenTelemetry spans
- SQL query logs

It is still difficult to answer all of the following questions in one place:

- Why was this request slow?
- Which dependency consumed the most time?
- Was validation, the handler, or serialization slow?
- How many SQL queries ran inside the endpoint?
- Is there a possible N+1 query problem?
- How much time was spent waiting for an external HTTP service?
- Did the same dependency execute more than once?
- How long did setup and cleanup take for a `yield` dependency?
- How much wall time did a synchronous endpoint spend in the thread pool?
- How should a streaming response be measured?

`fastapi-lens` should answer these questions through a FastAPI-aware execution
timeline while clearly distinguishing measured facts from heuristics.

---

## 3. Target Users

- Backend developers working with FastAPI
- Microservice teams
- Platform and SRE teams
- API teams diagnosing performance problems
- Teams using SQLAlchemy
- Developers analyzing requests in local and staging environments
- Teams that need fast diagnostics before adopting a complete APM stack

---

## 4. Project Goals

### 4.1 Primary goals

- Collect an execution timeline automatically for each profiled request
- Display the FastAPI dependency graph with timing information
- Support synchronous and asynchronous endpoints
- Associate SQLAlchemy queries with the active request
- Measure response serialization
- Require minimal integration work
- Produce HTML and JSON reports
- Provide safe defaults for development and staging use

### 4.2 Secondary goals

- Detect slow dependencies
- Detect repeated SQL statements
- Identify possible N+1 query patterns
- Measure outbound HTTP requests
- Integrate with Prometheus and OpenTelemetry
- Compare traces
- Detect regressions against a baseline

### 4.3 Non-goals for the first release

- A complete APM product
- A distributed tracing backend
- A log aggregation platform
- Persistent production trace storage
- User management or a multi-tenant dashboard
- A Kubernetes operator
- Automatic performance optimization
- AI-based root-cause analysis

---

## 5. MVP Scope

### 5.1 v0.1 support matrix

| Area | v0.1 support level |
|---|---|
| Request lifecycle | Measured through explicit ASGI checkpoints |
| Endpoint handler | Synchronous and asynchronous wall-clock time |
| FastAPI dependencies | Setup and cleanup phases measured separately |
| Nested dependencies | Logical dependency graph |
| Dependency cache | Hit, miss, and `use_cache=False` metadata |
| SQLAlchemy | Queries from explicitly registered sync and async engines |
| Response serialization | One reliable combined segment |
| Streaming responses | Response creation and body transmission separated |
| Route filtering | Raw-path prefilter and route-template final filter |
| Output | Versioned JSON schema and a small HTML dashboard |
| Diagnostics | Slow request/dependency, repeated query, possible N+1, expensive serialization |
| Security | Dashboard off by default; redaction and limits applied before storage |

The following areas are not separate v0.1 execution segments:

- Per-middleware attribution
- Separate request-body read, parse, and Pydantic validation timing
- Outbound HTTP instrumentation
- Per-task background execution timelines
- Separate thread-pool queue wait and callable execution timing
- Per-chunk streaming analysis
- Request or response body capture

Some of this work may be included in `unattributed` or `post_response` time.
v0.1 must not present that time as a confirmed root cause.

### 5.2 Normative lifecycle checkpoints

The pure ASGI middleware must collect the following monotonic timestamps:

| Field | Definition |
|---|---|
| `request_received_ns` | The Lens ASGI middleware receives the HTTP scope |
| `response_started_ns` | Immediately before forwarding the first `http.response.start` event |
| `response_body_completed_ns` | The downstream `send` await returns for the final body event with `more_body=False` |
| `application_completed_ns` | The downstream ASGI application returns |

Derived durations:

- `time_to_response_start_ms`
- `response_complete_duration_ms`
- `response_send_duration_ms`
- `post_response_duration_ms`
- `application_duration_ms`

The dashboard's primary request duration is
`response_complete_duration_ms`. This is a server-side ASGI measurement and
does not prove that the bytes physically reached the client at that instant.

Background tasks and request-scoped dependency cleanup can occur after the
response body completes. Even though v0.1 does not profile each background task,
that work remains visible in `post_response_duration_ms`.

If an exception, cancellation, or client disconnect prevents a checkpoint from
being observed, its derived duration must be `null`. The profiler must not
estimate missing timestamps.

---

## 6. Usage Examples

### 6.1 Installation

```bash
pip install fastapi-lens
```

Install SQLAlchemy support:

```bash
pip install "fastapi-lens[sqlalchemy]"
```

Install development dependencies:

```bash
pip install "fastapi-lens[dev]"
```

### 6.2 Basic usage

```python
from fastapi import FastAPI
from fastapi_lens import Lens

app = FastAPI()

lens = Lens(
    app,
    enabled=True,
    dashboard_enabled=True,
    environment="development",
    dashboard_path="/__lens__",
    slow_request_threshold_ms=250,
)
```

### 6.3 Advanced usage

```python
from fastapi import FastAPI
from fastapi_lens import Lens, LensConfig

app = FastAPI()

config = LensConfig(
    enabled=True,
    dashboard_enabled=True,
    environment="development",
    dashboard_path="/__lens__",
    include_routes=["/api/*"],
    exclude_routes=["/health", "/metrics", "/docs", "/openapi.json"],
    slow_request_threshold_ms=250,
    capture_dependencies=True,
    capture_sql=True,
    capture_serialization=True,
    max_traces=500,
)

lens = Lens(app, config=config)
lens.instrument_sqlalchemy(engine)
```

`capture_sql=True` enables SQL support but does not discover engines. Every
synchronous or asynchronous engine must be registered explicitly.

---

## 7. Expected Profile Output

```text
GET /customers/42
├── Response complete duration             286 ms
├── Response start                         241 ms
├── Response body send                      45 ms
├── Post-response work                       8 ms
└── Application duration                   294 ms

Measured execution
├── Dependency setup                        46 ms wall time
│   ├── authenticate_user.setup             18 ms
│   ├── load_permissions.setup              21 ms
│   └── get_database_session.setup           7 ms
├── Endpoint handler                       173 ms
│   └── PostgreSQL query                    82 ms
├── Response serialization                  39 ms
└── Unattributed                            28 ms

Post-response execution
├── get_database_session.cleanup             1 ms
└── Unattributed                             7 ms
```

Segments may overlap or be nested. Child durations are not required to add up
to the parent wall-clock duration. The dashboard should display wall-clock time
and, when it can be calculated reliably, exclusive or self time.

Example JSON:

```json
{
  "schema_version": "1.0",
  "trace_id": "01J...",
  "method": "GET",
  "route": "/customers/{customer_id}",
  "path": "/customers/42",
  "status_code": 200,
  "started_at": "2026-08-02T20:00:00.000Z",
  "response_complete_duration_ms": 286.4,
  "application_duration_ms": 294.1,
  "post_response_duration_ms": 7.7,
  "complete": true,
  "segments": [
    {
      "type": "dependency_setup",
      "name": "authenticate_user",
      "duration_ms": 18.2,
      "parent_id": null,
      "attributes": {
        "cache_status": "miss",
        "scope": "request"
      }
    },
    {
      "type": "sql",
      "name": "SELECT customers",
      "duration_ms": 82.1,
      "parent_id": "handler"
    }
  ],
  "diagnostics": [
    {
      "code": "SLOW_DEPENDENCY",
      "severity": "warning",
      "message": "authenticate_user setup consumed 6.4% of response-complete duration."
    }
  ]
}
```

---

## 8. Functional Requirements

### FR-001 — Application integration

The library must attach to an existing FastAPI application through one object:

```python
Lens(app)
```

### FR-002 — Request traces

Every profiled request must produce a unique trace with at least:

- JSON schema version
- Trace ID
- HTTP method
- Route template
- Actual path
- Status code
- UTC start time
- Lifecycle checkpoints and derived durations
- Completion state
- Segment list
- Redacted structured error information
- Diagnostic list

### FR-003 — Route filtering

Users must be able to define include and exclude patterns:

```python
Lens(
    app,
    include_routes=["/api/*"],
    exclude_routes=["/health", "/metrics"],
)
```

Exclude rules take precedence over include rules.

Patterns use POSIX-like glob semantics. `*` matches one or more path
characters. The profiler first applies a raw-path prefilter using
`scope["path"]`, then performs the final decision against the route template
after routing. A trace rejected by the final filter is discarded before
storage.

Tests must cover `root_path`, mounted applications, trailing slashes, 404
responses, and 405 responses.

### FR-004 — Dependency profiling

The profiler must support:

- Synchronous dependencies
- Asynchronous dependencies
- Nested dependencies
- Callable class dependencies
- `yield` dependencies
- Cached dependencies
- `use_cache=False`
- Dependency exceptions

A `yield` dependency must not be represented as one long segment. Setup before
the yield and cleanup after the yield are separate execution segments. The
dashboard groups them under one logical dependency node.

A cache hit does not execute the callable. It therefore produces logical
metadata with `cache_status="hit"` rather than a fake zero-duration execution
segment.

### FR-005 — Handler profiling

The endpoint function must have a separate segment for:

- `async def` endpoints
- `def` endpoints
- Endpoints that raise exceptions
- Endpoints returning custom responses
- Endpoints returning streaming responses

For streaming responses, handler timing ends when the response object is
created. Stream consumption is measured by lifecycle checkpoints. v0.1 must not
label generator consumption as handler execution.

### FR-006 — SQLAlchemy profiling

Each SQL segment should include, when available:

- SQL operation
- Normalized statement
- Stable statement fingerprint
- Duration
- Database dialect
- Success or error state
- Row count
- Parent execution segment

Bind values are not stored by default.

SQL capture applies only to explicitly registered engines:

```python
lens.instrument_sqlalchemy(engine)
lens.uninstrument_sqlalchemy(engine)
```

For `AsyncEngine`, listeners target `engine.sync_engine`. Registration is
idempotent and reference-counted. Multiple Lens instances in the same process
must not disable one another.

Connection pre-ping and framework-internal operations must not be classified as
user queries. Query start/end correlation must be stack-safe and associated
with the connection or execution context rather than a global variable.

### FR-007 — Serialization profiling

Where possible, serialization may be separated into:

- Response validation
- JSON encoding
- Response rendering

The v0.1 acceptance criterion is one reliable combined serialization segment.
Sub-phases may be exposed only when a supported FastAPI adapter can separate
them without changing application behavior.

### FR-008 — Trace storage

v0.1 uses an in-process bounded store:

```python
MemoryTraceStore(max_traces=500)
```

The oldest trace is removed when capacity is reached.

Only finalized immutable snapshots may be stored. In addition to `max_traces`,
the implementation must limit segment count, attribute size, SQL length, error
length, API page size, and approximate serialized bytes per trace.

In multi-worker deployments, each worker has an independent store and route
summary. The dashboard must make this limitation visible.

### FR-009 — Dashboard

The dashboard must include:

- Recent requests
- Route summary
- Request-detail timeline
- Dependency graph
- SQL queries
- Diagnostics
- Error information

### FR-010 — JSON API

```text
GET /__lens__/api/traces
GET /__lens__/api/traces/{trace_id}
GET /__lens__/api/routes
DELETE /__lens__/api/traces
```

List endpoints require stable ordering, validated pagination, and a maximum page
size. The clear endpoint uses the same authorization policy as the dashboard.
Cookie-based authorization also requires CSRF protection.

### FR-011 — Slow request classification

A trace is marked slow when its response-complete duration reaches the
configured threshold.

### FR-012 — Exception capture

Requests that raise exceptions must still be traced. Instrumentation must not
change the application's existing exception behavior.

### FR-013 — Runtime control

The profiler can be disabled through runtime configuration or:

```bash
FASTAPI_LENS_ENABLED=false
```

Precedence:

```text
runtime enable/disable > explicit LensConfig > environment variable > default
```

Disabling stops creation of new traces. Active traces are allowed to finalize
safely.

### FR-014 — Custom segments

Asynchronous usage:

```python
from fastapi_lens import current_trace

async with current_trace.segment("calculate_price"):
    result = await calculate_price()
```

Synchronous usage:

```python
with current_trace.segment_sync("calculate_price"):
    result = calculate_price()
```

---

## 9. Non-Functional Requirements

### NFR-001 — Performance overhead

With SQL capture disabled:

- Target p50 overhead: below 5%
- Target p95 overhead: below 10%

Percentage targets are not sufficient by themselves. Benchmarks must also
report absolute added latency per request in microseconds.

The benchmark report must fix and publish:

- Hardware and operating system
- Python, FastAPI, Starlette, Pydantic, and SQLAlchemy versions
- Endpoint workload
- Warm-up policy
- Iteration count
- Concurrency
- Sampling and capture settings

Percentage overhead can be misleading for very fast no-op endpoints and must be
interpreted with the absolute result.

### NFR-002 — Security

The following are never stored by default:

- Authorization headers
- Cookies
- Request bodies
- Response bodies
- SQL bind values
- Passwords, tokens, or secrets

Limits are applied before storage:

- Maximum segments per trace
- Maximum segment-name and attribute length
- Maximum serialized bytes per trace
- Maximum exception message and stack length
- Maximum dashboard API page size

### NFR-003 — Failure isolation

Request-time profiler failures must not break the application request.
Instrumentation failures are logged internally with rate limiting.

Invalid configuration, an unsupported FastAPI version, or failure to install a
required adapter may fail fast during application initialization. Silent partial
instrumentation is not acceptable.

### NFR-004 — Concurrency safety

Request context is isolated with `contextvars.ContextVar`. Concurrent requests
must never mix segments.

The lifecycle middleware must be pure ASGI middleware.
`BaseHTTPMiddleware` is not used by the core tracing implementation because of
its context propagation limitations.

### NFR-005 — Python support

- Python 3.11
- Python 3.12
- Python 3.13

### NFR-006 — FastAPI compatibility

The initial supported FastAPI range is selected after the feasibility spike.
All code that depends on FastAPI internals must be isolated behind versioned
adapters.

### NFR-007 — Type safety

The complete public API must have type hints. The package includes `py.typed`.

### NFR-008 — Test coverage

Core modules target at least 85% branch coverage.

### NFR-009 — Project language

English is the only language used in the repository. This rule applies to:

- Source code identifiers and comments
- Documentation
- User interface text
- API messages and errors
- Logs and diagnostics
- Tests, fixtures, and examples
- Configuration descriptions
- Commit messages and pull-request content

Language checks should run in CI. A small allowlist may be used only for test
fixtures that explicitly verify Unicode or localization behavior.

---

## 10. Proposed Architecture

```text
fastapi-lens/
├── src/
│   └── fastapi_lens/
│       ├── __init__.py
│       ├── lens.py
│       ├── config.py
│       ├── context.py
│       ├── models.py
│       ├── middleware.py
│       ├── instrumentation/
│       │   ├── __init__.py
│       │   ├── fastapi.py
│       │   ├── dependencies.py
│       │   ├── serialization.py
│       │   └── sqlalchemy.py
│       ├── diagnostics/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── slow_dependency.py
│       │   ├── duplicate_query.py
│       │   └── expensive_serialization.py
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   └── memory.py
│       ├── dashboard/
│       │   ├── __init__.py
│       │   ├── router.py
│       │   ├── schemas.py
│       │   ├── static/
│       │   └── templates/
│       ├── exporters/
│       │   ├── __init__.py
│       │   ├── json.py
│       │   └── console.py
│       └── utils/
│           ├── timing.py
│           ├── patterns.py
│           └── redaction.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── compatibility/
│   ├── benchmarks/
│   └── spike/
├── examples/
│   ├── basic/
│   ├── dependencies/
│   ├── sqlalchemy/
│   └── custom_segments/
├── docs/
│   ├── adr/
│   ├── architecture.md
│   ├── instrumentation.md
│   ├── security.md
│   └── compatibility.md
├── pyproject.toml
├── README.md
├── LICENSE
├── CONTRIBUTING.md
└── CHANGELOG.md
```

---

## 11. Core Domain Models

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias


JsonValue: TypeAlias = (
    bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
)
FrozenJsonValue: TypeAlias = (
    bool
    | int
    | float
    | str
    | tuple["FrozenJsonValue", ...]
    | tuple[tuple[str, "FrozenJsonValue"], ...]
    | None
)


class SegmentType(StrEnum):
    DEPENDENCY_SETUP = "dependency_setup"
    DEPENDENCY_CLEANUP = "dependency_cleanup"
    HANDLER = "handler"
    SQL = "sql"
    SERIALIZATION = "serialization"
    RESPONSE_SEND = "response_send"
    CUSTOM = "custom"


class SegmentStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"


@dataclass(slots=True, frozen=True)
class TraceError:
    type: str
    message: str
    stack: str | None = None


@dataclass(slots=True, frozen=True)
class Diagnostic:
    code: str
    severity: str
    message: str
    segment_id: str | None = None


@dataclass(slots=True)
class TraceSegment:
    id: str
    trace_id: str
    type: SegmentType
    name: str
    start_ns: int
    end_ns: int | None = None
    parent_id: str | None = None
    logical_dependency_id: str | None = None
    status: SegmentStatus = SegmentStatus.INCOMPLETE
    attributes: dict[str, JsonValue] = field(default_factory=dict)
    error: TraceError | None = None

    @property
    def duration_ms(self) -> float | None:
        if self.end_ns is None:
            return None
        return (self.end_ns - self.start_ns) / 1_000_000


@dataclass(slots=True)
class RequestTrace:
    schema_version: str
    id: str
    method: str
    path: str
    started_at: datetime
    request_received_ns: int
    route: str | None = None
    response_started_ns: int | None = None
    response_body_completed_ns: int | None = None
    application_completed_ns: int | None = None
    status_code: int | None = None
    segments: list[TraceSegment] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    error: TraceError | None = None
    complete: bool = False

    @property
    def response_complete_duration_ms(self) -> float | None:
        if self.response_body_completed_ns is None:
            return None
        return (self.response_body_completed_ns - self.request_received_ns) / 1_000_000

    @property
    def application_duration_ms(self) -> float | None:
        if self.application_completed_ns is None:
            return None
        return (self.application_completed_ns - self.request_received_ns) / 1_000_000


@dataclass(slots=True, frozen=True)
class TraceSegmentSnapshot:
    id: str
    trace_id: str
    type: SegmentType
    name: str
    start_ns: int
    end_ns: int | None
    parent_id: str | None
    logical_dependency_id: str | None
    status: SegmentStatus
    attributes: tuple[tuple[str, FrozenJsonValue], ...]
    error: TraceError | None


@dataclass(slots=True, frozen=True)
class RequestTraceSnapshot:
    schema_version: str
    id: str
    method: str
    path: str
    route: str | None
    started_at: datetime
    request_received_ns: int
    response_started_ns: int | None
    response_body_completed_ns: int | None
    application_completed_ns: int | None
    status_code: int | None
    segments: tuple[TraceSegmentSnapshot, ...]
    diagnostics: tuple[Diagnostic, ...]
    error: TraceError | None
    complete: bool
```

Use `time.perf_counter_ns()` for durations. Wall-clock time is used only for a
timezone-aware UTC `started_at`.

Mutable models exist only inside the request collector. Before storage or
export, the collector produces a deep-frozen `RequestTraceSnapshot`. Mutable
JSON lists and objects are converted into immutable tuple-based values.

`complete` is true only when the response body completed, the downstream ASGI
application returned, and no collector integrity failure occurred. A handled
exception may still produce a complete trace. A missing response or
cancellation produces an incomplete trace.

Execution segments form an execution tree. The logical dependency graph is a
separate view because multiple callers may reference the same cached dependency.

---

## 12. Request Context Design

The active collector and segment stack use separate context variables:

```python
from contextvars import ContextVar

_current_collector: ContextVar["TraceCollector | None"] = ContextVar(
    "fastapi_lens_current_collector",
    default=None,
)
_segment_stack: ContextVar[tuple[str, ...]] = ContextVar(
    "fastapi_lens_segment_stack",
    default=(),
)
```

Rules:

- Pure ASGI middleware creates the collector.
- Context tokens are retained and reset reliably.
- Reset happens after the downstream app returns and the snapshot is finalized.
- Child-task propagation is tested.
- Thread-pool propagation is tested for sync endpoints and dependencies.
- Instrumentation is a no-op without an active collector.
- Parallel child tasks use independent immutable stack copies.
- Writes to the shared collector are event-loop and thread-pool safe.
- Late segments cannot mutate a finalized snapshot.

---

## 13. Instrumentation Strategy

### 13.1 Pure ASGI lifecycle middleware

Responsibilities:

- Handle only HTTP scopes
- Apply the raw-path prefilter
- Create the root trace
- Wrap `receive` and `send`
- Record lifecycle checkpoints
- Record status and completion state
- Apply the final route-template filter
- Capture redacted error information
- Run diagnostics
- Store an immutable snapshot on a best-effort basis
- Reset context

The core implementation must not use `BaseHTTPMiddleware`. Missing checkpoints
remain null for disconnect, cancellation, exception, empty-body, streaming, and
custom ASGI response cases.

### 13.2 FastAPI dependency instrumentation

The feasibility spike must compare:

- Versioned integration around `solve_dependencies`
- Route-specific wrapping of callables in the `Dependant` graph

Any global patch must be:

- Isolated behind a versioned adapter
- Idempotent and reference-counted
- Safe with multiple applications and Lens instances
- Restored only when the last registration is removed
- Thread-safe during install and restore
- Covered by compatibility tests
- Rejected clearly for unsupported FastAPI versions

Instrumentation must preserve:

- Callable identity and signature
- Dependency cache keys
- Generator teardown order
- Exception propagation
- `scope="function"` behavior
- `scope="request"` behavior

Setup and cleanup are separate segments. Cleanup preserves FastAPI's LIFO
teardown order.

### 13.3 Handler instrumentation

The handler segment wraps the route handler call. Context and timing must be
preserved when a sync handler runs in the thread pool.

v0.1 reports sync handler wall-clock time. Unless separately measured, it must
not claim to distinguish queue wait from callable execution.

### 13.4 Serialization instrumentation

Candidate boundaries include:

- `serialize_response`
- Response-model validation
- `jsonable_encoder`
- Response-class rendering

One reliable combined serialization segment is sufficient for v0.1. Custom and
streaming responses may legitimately have no serialization segment. That state
is represented as not applicable rather than zero milliseconds.

### 13.5 SQLAlchemy instrumentation

Use SQLAlchemy events:

- `before_cursor_execute`
- `after_cursor_execute`
- `handle_error`

Listeners are added only through `instrument_sqlalchemy`. Async engines use
their synchronous proxy engine as the event target.

Without an active collector, event listeners immediately return.

SQL handling:

- Collapse redundant whitespace
- Mask literal values where safely possible
- Never persist bind values by default
- Truncate display SQL
- Produce a stable untruncated fingerprint for comparisons
- Store `executemany`, dialect, and reliable row count as metadata
- Exclude pre-ping and framework-internal operations

### 13.6 Timeline mathematics

- Segment duration is wall-clock time.
- Parent-child relationships describe call context, not additive accounting.
- Parallel child segments can overlap and exceed the parent when summed.
- Self time is shown only when child interval union can be subtracted reliably.
- Unattributed time subtracts the union of measured top-level intervals.
- Percentage diagnostics state their denominator explicitly.
- Negative or out-of-lifecycle intervals produce integrity diagnostics.
- The UI must not silently clamp invalid intervals to zero.

---

## 14. Diagnostic System

Diagnostics run after finalization:

```python
class DiagnosticRule(Protocol):
    code: str

    def evaluate(self, trace: RequestTraceSnapshot) -> list[Diagnostic]: ...
```

### 14.1 Slow request

```text
trace.response_complete_duration_ms >= slow_request_threshold_ms
```

### 14.2 Slow dependency

A setup or cleanup segment exceeds an absolute threshold or a configured
percentage of response-complete or application duration.

### 14.3 Repeated query

The same `statement_fingerprint` appears multiple times. This reports a repeat;
it does not claim that the repeat is necessarily incorrect.

### 14.4 Possible N+1

Initial heuristic:

- The same fingerprint executes at least ten times.
- Parameter sets differ, without storing the bind values.
- The group consumes a meaningful portion of total SQL time.

The message must explicitly say that the finding is heuristic.

### 14.5 Expensive serialization

Serialization exceeds an absolute threshold or a configured percentage of
response-complete duration.

### 14.6 Data integrity

Invalid intervals, overflow, truncation, late segments, or missing expected
checkpoints can produce internal data-integrity diagnostics.

---

## 15. Configuration Model

```python
from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class LensConfig:
    enabled: bool = True
    dashboard_enabled: bool = False
    dashboard_path: str = "/__lens__"
    include_routes: list[str] = field(default_factory=lambda: ["*"])
    exclude_routes: list[str] = field(
        default_factory=lambda: [
            "/docs",
            "/redoc",
            "/openapi.json",
            "/health",
            "/metrics",
            "/__lens__*",
        ]
    )
    slow_request_threshold_ms: float = 250.0
    slow_dependency_threshold_ms: float = 100.0
    capture_dependencies: bool = True
    capture_sql: bool = False
    capture_serialization: bool = True
    max_traces: int = 500
    max_segments_per_trace: int = 1_000
    max_attribute_length: int = 2_000
    max_error_length: int = 8_000
    max_trace_bytes: int = 1_000_000
    max_api_page_size: int = 200
    max_sql_length: int = 2_000
    environment: Literal["development", "test", "staging", "production"] | None = None
    allow_in_production: bool = False
```

The dashboard is disabled by default. Enabling it requires an explicit
environment. Staging and production additionally require
`allow_in_production=True` and an authorization dependency.

The library must not attempt to infer the deployment environment.

Environment parsing belongs in a separate tested config loader. v0.1 supports
at least `FASTAPI_LENS_ENABLED`. Full environment-variable coverage is deferred
to v0.2. Invalid values must not silently fall back to defaults.

---

## 16. Security Requirements

### 16.1 Default redaction

Sensitive headers:

- `authorization`
- `proxy-authorization`
- `cookie`
- `set-cookie`
- `x-api-key`

Sensitive field names:

- `password`
- `passwd`
- `secret`
- `token`
- `access_token`
- `refresh_token`
- `api_key`
- `private_key`

### 16.2 Dashboard access

- The dashboard is disabled by default.
- Development and test require explicit enablement.
- Staging and production require explicit override and authorization.
- Authorization applies to HTML, JSON, and state-changing endpoints.
- Loopback filtering is a convenience check, not an authorization boundary.
- Cookie-based authentication requires CSRF protection for mutations.

```python
Lens(
    app,
    config=LensConfig(
        dashboard_enabled=True,
        environment="staging",
        allow_in_production=True,
    ),
    dashboard_dependencies=[Depends(require_admin)],
)
```

### 16.3 Data storage

v0.1 stores data only in process memory. Data disappears when the process
stops.

Redaction and size limits run before storage. SQL statements, paths, custom
segment names, arbitrary attributes, diagnostics, and exception messages are
all untrusted data.

### 16.4 Dashboard output safety

- Keep Jinja2 autoescaping enabled.
- Never interpolate raw JSON into executable script text.
- Never render SQL, paths, diagnostics, or errors as trusted HTML.
- Send a restrictive Content Security Policy.
- Send `Cache-Control: no-store`.
- Authorization failures must not leak trace data.

---

## 17. Dashboard MVP

The first UI uses server-rendered HTML, Jinja2, and minimal vanilla JavaScript.
It must not require a frontend build step.

### 17.1 Request list

- Start time
- Method
- Route
- Status
- Response-complete duration
- Post-response duration
- SQL query count
- Dependency count
- Diagnostic count

### 17.2 Request detail

- General metadata
- Lifecycle checkpoints
- Waterfall timeline
- Dependency graph
- SQL table
- Diagnostics
- Error information
- Self and unattributed time

### 17.3 Route summary

- Request count
- Average
- Minimum and maximum
- p50
- p95
- p99
- Error count

Percentiles use complete traces grouped by route template and based on
`response_complete_duration_ms`. The UI states that process-local data does not
represent other workers.

---

## 18. Public API Draft

```python
from fastapi_lens import Lens, LensConfig

lens = Lens(app, config=LensConfig(...))

lens.enable()
lens.disable()
lens.instrument_sqlalchemy(engine)
lens.uninstrument_sqlalchemy(engine)

await lens.clear_traces()
trace = await lens.get_trace(trace_id)
traces = await lens.list_traces(limit=100, offset=0)
```

Enable and disable are synchronous policy changes. Store access is
asynchronous. Active requests still finalize after disable.

Decorator:

```python
from fastapi_lens import trace_segment


@trace_segment("calculate_invoice")
async def calculate_invoice(...):
    ...
```

Context manager:

```python
from fastapi_lens import current_trace

async with current_trace.segment("external_operation"):
    ...
```

Decorators support both synchronous and asynchronous callables.
`current_trace` is a no-op proxy when no trace is active, so callers do not need
to check for `None`.

---

## 19. Storage Interface

```python
from typing import Protocol


class TraceStore(Protocol):
    async def save(self, trace: RequestTraceSnapshot) -> None: ...

    async def get(self, trace_id: str) -> RequestTraceSnapshot | None: ...

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RequestTraceSnapshot]: ...

    async def clear(self) -> None: ...
```

v0.1 implementation:

```python
MemoryTraceStore
```

Rules:

- `save` accepts finalized immutable snapshots only.
- Ordering uses `application_completed_ns`, then trace ID.
- Pagination is validated against the configured maximum.
- The memory store is thread-safe and async-task-safe.
- Store failures do not affect the request.
- Failures produce rate-limited internal logs and a dropped-trace metric.
- The memory save may be awaited at request completion.
- Future network stores use a bounded queue with an explicit drop or
  backpressure policy.
- Storage remains process-local in multi-process deployments.

Future stores:

- Redis
- SQLite
- PostgreSQL
- OpenTelemetry exporter

---

## 20. Test Strategy

### 20.1 Unit tests

- Timing helpers
- Route matching
- Redaction
- Ring-buffer behavior
- Stable storage ordering
- Lifecycle duration properties
- Missing checkpoints
- Parent-child relationships
- Overlap, self-time, and unattributed-time calculations
- Diagnostics
- Configuration validation
- SQL normalization and fingerprints
- Trace limits
- Snapshot immutability

### 20.2 Integration tests

- Async endpoint
- Sync endpoint
- Nested dependency
- Cached dependency
- `use_cache=False`
- Async generator dependency
- Sync generator dependency
- `yield` with function scope
- `yield` with request scope
- LIFO cleanup
- Dependency exception
- Endpoint exception
- Response model
- Custom response
- Streaming response
- Background task contribution to post-response duration
- Client disconnect
- Cancellation
- Pure ASGI checkpoints
- Coexistence with application `BaseHTTPMiddleware`
- SQLAlchemy sync engine
- SQLAlchemy async engine
- SQLAlchemy pre-ping filtering
- Multiple applications and Lens instances
- Idempotent install and restore
- Concurrent request isolation
- Dashboard authorization
- CSRF behavior
- Output escaping
- CSP and no-store headers
- Trace limits

### 20.3 Compatibility tests

- Supported Python versions
- Each supported FastAPI adapter range
- Compatible Starlette versions
- Pydantic v2
- SQLAlchemy 2.x
- Latest FastAPI as a separate compatibility signal

The latest-version signal may be informational until an adapter is declared
supported. Supported ranges must be blocking CI jobs.

### 20.4 Benchmarks

Scenarios:

1. Plain FastAPI baseline
2. Lens enabled with minimum capture
3. Dependency capture enabled
4. SQL capture enabled
5. Dashboard enabled but idle
6. 100 concurrent requests
7. Sync endpoint
8. Async endpoint

Report:

- Requests per second
- p50, p95, and p99 latency
- Absolute added latency per request
- Memory use
- Approximate memory per trace
- Dropped or incomplete trace count

---

## 21. MVP Acceptance Criteria

- [x] The package installs successfully.
- [x] `Lens(app)` attaches to FastAPI.
- [x] Pure ASGI middleware records all lifecycle checkpoints correctly.
- [x] Missing responses and disconnects do not produce invented durations.
- [x] Async handlers are profiled correctly.
- [x] Sync handlers are profiled correctly.
- [x] Nested dependency relationships are correct.
- [x] `yield` setup and cleanup are separate.
- [x] Function and request dependency scopes are correct.
- [x] Cached dependency metadata is correct without re-execution.
- [x] Concurrent requests never mix trace data.
- [x] Endpoint errors do not change application behavior.
- [x] Only explicitly registered SQLAlchemy engines are captured.
- [x] Sync and async engine registration is idempotent and removable.
- [x] SQL bind values are not stored.
- [x] Serialization is measured reliably.
- [x] Streaming response creation and transmission are separated.
- [x] JSON includes `schema_version` and lifecycle durations.
- [x] JSON trace endpoints work.
- [x] The HTML dashboard lists requests.
- [x] Request detail displays segments.
- [x] Include and exclude filters work.
- [x] Disable stops new traces and safely finalizes active traces.
- [x] The dashboard is off by default.
- [x] Staging and production require authorization.
- [x] Redaction and limits run before storage.
- [x] Escaping, CSP, no-store, and mutation protection are tested.
- [x] Stored snapshots cannot be mutated.
- [x] The coverage target is met.
- [x] README quick-start examples work.
- [x] Percentage and absolute benchmark results are documented.
- [x] The supported FastAPI adapter range is documented.
- [x] The package is ready for TestPyPI.

---

## 22. Development Phases

### Phase 0 — Bootstrap and feasibility

- Minimal repository and test harness
- Pure ASGI lifecycle prototype
- FastAPI request lifecycle research
- Comparison of dependency instrumentation approaches
- Sync thread-pool context tests
- `yield` scope, cache, setup, and cleanup prototypes
- Serialization boundary prototype
- SQLAlchemy explicit engine registration prototype
- Multi-application install and restore tests
- Initial supported FastAPI range
- Architecture decision record

Required outputs:

```text
docs/adr/0001-instrumentation-strategy.md
tests/spike/
```

TASK-002 must not begin until the spike proves usable lifecycle, dependency,
serialization, and SQLAlchemy boundaries. Spike code is disposable research
code and must not be copied into the production package without deliberate
redesign.

### Phase 1 — Core tracing

- Domain models
- Context management
- Root lifecycle trace
- Custom segment API
- Memory storage
- Route filtering
- Error handling
- Redaction
- Bounded collector
- Dashboard production guard foundation
- Versioned JSON output

### Phase 2 — FastAPI-aware instrumentation

- Handler timing
- Sync and async support
- Dependency timing
- Logical dependency graph
- Generator setup and cleanup
- Serialization timing

### Phase 3 — SQLAlchemy

- Explicit engine registration
- Sync and async engines
- Query normalization and fingerprints
- Timing and error capture
- Listener restore lifecycle
- Repeated-query diagnostics

### Phase 4 — Dashboard

- Authorization and output security
- Internal API
- Request list and details
- Waterfall
- Dependency graph
- SQL table
- Route summaries

### Phase 5 — Quality and release

- Compatibility matrix
- Benchmarks
- Security review
- Documentation
- Example applications
- GitHub Actions
- TestPyPI
- PyPI

---

## 23. Implementation Tasks

### TASK-001 — Repository bootstrap

- Create a `src` layout.
- Add `pyproject.toml`.
- Configure Ruff, mypy, pytest, and coverage.
- Add the MIT license.
- Add basic CI.
- Create the package skeleton.

Acceptance:

- `pytest` passes.
- `ruff check .` passes.
- `mypy src` passes.
- Editable installation works.

### TASK-001A — Instrumentation feasibility spike

- Prototype pure ASGI checkpoints.
- Compare dependency instrumentation approaches.
- Observe dependency caching and `use_cache=False`.
- Prototype `yield` setup, cleanup, and scopes.
- Verify sync handler context propagation.
- Prototype serialization boundaries.
- Prototype sync and async SQLAlchemy registration.
- Test multi-application install and restore.
- Propose the first compatibility range.
- Write `docs/adr/0001-instrumentation-strategy.md`.

Acceptance:

- Prototypes run under `tests/spike/`.
- Normal, streaming, exception, and disconnect cases are observed.
- Dependency identity, caching, and teardown remain unchanged.
- The ADR records the adapter, supported range, and fallback behavior.
- A clear go or no-go decision exists before TASK-002.

### TASK-002 — Trace domain models

- `RequestTrace`
- `TraceSegment`
- `RequestTraceSnapshot`
- `TraceError`
- `Diagnostic`
- Enums
- Lifecycle properties
- Logical dependency nodes
- Interval calculations
- Versioned bounded JSON
- Immutable snapshots

### TASK-003 — Request context

- Collector context variable
- Immutable segment stack
- Set and reset helpers
- Parent-child relationships
- No-op behavior
- Thread-pool-safe writes
- Late-segment behavior

### TASK-004 — Memory trace store

- Thread-safe and async-safe bounded storage
- Save, get, list, and clear
- Oldest-item eviction
- Stable ordering
- Pagination limits
- Immutable snapshot enforcement
- Process-local limitation documentation

### TASK-005 — Pure ASGI lifecycle middleware

- Four lifecycle checkpoints
- `receive` and `send` wrappers
- Status and completion state
- Error capture
- Finalization and diagnostic execution
- Best-effort storage
- Two-stage route filtering
- Enabled state
- Disconnect, cancellation, and streaming tests

### TASK-006 — Custom segment API

- Sync context manager
- Async context manager
- Sync and async decorators
- Nested segment tests
- Exception-safe closure

### TASK-007 — Handler instrumentation

- Async handler timing
- Sync handler timing
- Context propagation
- Sync wall-clock labeling
- Streaming response creation boundary
- Exception tests

### TASK-008 — Dependency instrumentation

- Sync and async dependencies
- Nested dependencies
- Cached dependencies
- `use_cache=False`
- Callable classes
- Generator dependencies
- Function and request scopes
- Separate setup and cleanup
- Cache metadata
- LIFO teardown
- Exception behavior

### TASK-009 — Serialization instrumentation

- Confirm the serialization boundary.
- Test response models.
- Test JSON responses.
- Define not-applicable custom and streaming responses.
- Test errors.
- Document streaming behavior.

### TASK-010 — SQLAlchemy integration

- Public registration and removal API
- Idempotent reference-counted listeners
- Query start and end
- Error handling
- Pre-ping filtering
- Normalization and fingerprints
- Request association
- Sync and async engines
- Multi-application restore tests

### TASK-011 — Diagnostic engine

- Rule protocol
- Slow request
- Slow dependency
- Repeated query
- Possible N+1
- Expensive serialization
- Integrity and incomplete-trace diagnostics
- Explicit denominators
- Overlap-aware behavior

### TASK-012 — Security foundation

- Redaction before storage
- Trace and field limits
- SQL bind exclusion
- Dashboard default-off behavior
- Production guard
- Authorization contract
- CSRF policy
- Safe rendering helpers
- CSP
- `Cache-Control: no-store`
- Security tests

This task must complete before dashboard implementation. A separate security
review still runs before release.

### TASK-013 — Dashboard API

- Trace list
- Trace detail
- Route summary
- Clear operation
- Response schemas
- Authorization
- Pagination limits
- Schema version
- Process-local indicator

### TASK-014 — Dashboard UI

- Request list
- Trace details
- Waterfall
- Dependency graph
- SQL table
- Diagnostics
- Lifecycle and post-response metrics
- Self and unattributed time
- Escaping and CSP validation

### TASK-015 — Benchmarks

- Baseline app
- Instrumented app
- Reproducible commands
- Environment metadata
- Percentage overhead
- Absolute overhead
- Results document

### TASK-016 — Documentation and release

- README
- Quick start
- Configuration reference
- Security guide
- SQLAlchemy example
- Dependency example
- Changelog
- TestPyPI workflow
- PyPI workflow

---

## 24. Git Strategy

Example branches:

```text
feat/core-tracing
feat/dependency-instrumentation
feat/sqlalchemy-integration
feat/dashboard
fix/context-propagation
docs/security-model
```

Example commits:

```text
feat: add request trace context
feat: instrument nested FastAPI dependencies
feat: add SQLAlchemy query timing
fix: preserve trace context in sync endpoints
test: cover concurrent request isolation
docs: document production security controls
```

With an issue ID:

```text
feat: [FL-12] instrument nested dependencies
```

---

## 25. Initial Codex Prompt

```text
You are implementing an open-source Python package named `fastapi-lens`.

Read `docs/FASTAPI_REQUEST_EXECUTION_PROFILER.md` completely before making
changes.

Goal:
Bootstrap the repository and implement TASK-001 only.

Requirements:
- Use a `src` package layout.
- Support Python 3.11, 3.12, and 3.13.
- Use `pyproject.toml`.
- Add FastAPI as a runtime dependency.
- Treat the FastAPI version range as provisional. TASK-001A will determine the
  supported adapter range before release.
- Configure pytest, pytest-asyncio, coverage, Ruff, and mypy.
- Add a minimal `fastapi_lens` package with a public `__version__`.
- Add a smoke test that imports the package.
- Add GitHub Actions for lint, type checking, and tests.
- Use the MIT license.
- Do not implement profiler functionality yet.
- Keep the public API intentionally minimal.
- Document assumptions.
- Run all available checks.

Deliverables:
1. Repository structure
2. pyproject.toml
3. CI workflow
4. Initial package
5. Initial tests
6. README development instructions

At the end:
- Summarize changed files.
- List commands executed.
- Report test, lint, and type-check results.
- Note unresolved issues.
```

---

## 26. Technical Risks

### Risk 1 — FastAPI internal APIs

Dependency and serialization instrumentation may require internal APIs.

Mitigations:

- Isolated versioned adapters
- Compatibility tests
- Explicit supported ranges
- Fail-fast version guards
- Adapter selection by FastAPI version

### Risk 2 — Profiler overhead

Mitigations:

- `perf_counter_ns`
- Lightweight models
- Lazy attribute capture
- No body capture in v0.1
- Hard trace limits
- Future sampling
- Dashboard aggregation outside the request path

### Risk 3 — Context propagation

Mitigations:

- AnyIO and Starlette integration tests
- Isolated context-copy helpers when required
- Early concurrent-request tests
- Tests with application `BaseHTTPMiddleware`

### Risk 4 — Streaming responses

The handler can return before the stream is consumed.

Mitigations:

- Separate response creation from body transmission
- Pure ASGI send wrapper
- Explicit support documentation

### Risk 5 — Sensitive data capture

Mitigations:

- Secure defaults
- Redaction before storage
- Bounded collector
- No body capture in v0.1
- No SQL bind capture
- Dashboard off by default
- Production authorization
- Safe HTML and JSON rendering
- CSP and no-store headers

### Risk 6 — Global instrumentation lifecycle

FastAPI adapters and SQLAlchemy listeners can affect multiple applications in
one process.

Mitigations:

- Idempotent reference-counted registry
- Explicit ownership
- Thread-safe install and restore
- Active-request disable and restore tests
- Multi-application integration tests

### Risk 7 — Unbounded memory

A ring buffer limits trace count but not the size of one trace.

Mitigations:

- Segment, attribute, error, SQL, and serialized-byte limits
- Truncation and drop metadata
- Immutable snapshots
- Dashboard pagination maximum
- Dropped and truncated trace metrics

---

## 27. Future Releases

### v0.2

- HTTPX and HTTPX2 outbound request instrumentation
- Sampling
- Trace export
- Custom diagnostic rules
- Environment variables for all configuration fields

### v0.3

- OpenTelemetry exporter
- Prometheus metrics
- Redis trace store
- Trace comparison
- Baseline regression detection

### v0.4

- WebSocket profiling
- Per-task background timeline
- Advanced streaming analysis
- Separate request parsing and validation timing
- Middleware attribution
- Sync thread-pool queue-wait separation
- SQL call-site information
- Plugin API

### v1.0

- Stable public API
- Broad FastAPI compatibility matrix
- Production-safe sampling mode
- Extension SDK
- Detailed migration policy

---

## 28. Success Metrics

Technical:

- Profiler overhead
- Test coverage
- Number of supported FastAPI versions
- Open bug count
- Mean issue resolution time

Community:

- PyPI downloads
- GitHub stars
- Contributors
- Integration pull requests
- Documentation example usage

Product:

- Steps required to find a slow-request root cause
- Percentage of traces with useful diagnostics
- False-positive N+1 rate
- Time required to inspect a trace

---

## 29. Definition of Done

A task is complete only when:

- Implementation is complete.
- Public APIs have type hints.
- Unit or integration tests exist.
- Tests pass.
- Ruff passes.
- Mypy passes.
- Security impact is reviewed.
- Documentation is updated when needed.
- Compatibility impact is stated.
- The commit message follows the project convention.
- All added repository text is in English.

---

## 30. Initial Development Order

```text
TASK-001 Repository bootstrap
    ↓
TASK-001A Instrumentation feasibility spike and ADR
    ↓
GO OR NO-GO GATE
    ↓
TASK-002 Trace domain models
    ↓
TASK-003 Request context
    ↓
TASK-004 Memory trace store
    ↓
TASK-005 Pure ASGI lifecycle middleware
    ↓
TASK-006 Custom segment API
    ↓
TASK-007 Handler instrumentation
    ↓
TASK-008 Dependency instrumentation
    ↓
TASK-009 Serialization instrumentation
    ↓
TASK-010 SQLAlchemy integration
    ↓
TASK-011 Diagnostics
    ↓
TASK-012 Security foundation
    ↓
TASK-013 Dashboard API
    ↓
TASK-014 Dashboard UI
    ↓
TASK-015 Benchmarks
    ↓
TASK-016 Documentation and release
```

TASK-001A is mandatory. TASK-002 must not begin without the ADR go or no-go
decision. If dependency or serialization instrumentation is not sustainable,
remove that feature from v0.1 rather than shaping the domain model around an
unproven internal API strategy.

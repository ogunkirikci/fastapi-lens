"""Core trace domain models."""

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


def freeze_json_value(value: JsonValue) -> FrozenJsonValue:
    """Recursively convert mutable JSON containers into immutable tuples."""
    if isinstance(value, list):
        return tuple(freeze_json_value(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            (key, freeze_json_value(item)) for key, item in sorted(value.items())
        )
    return value


class SegmentType(StrEnum):
    """A measured phase or operation within a request trace."""

    DEPENDENCY_SETUP = "dependency_setup"
    DEPENDENCY_CLEANUP = "dependency_cleanup"
    HANDLER = "handler"
    SQL = "sql"
    SERIALIZATION = "serialization"
    RESPONSE_SEND = "response_send"
    CUSTOM = "custom"


class SegmentStatus(StrEnum):
    """The terminal or current state of a trace segment."""

    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"


class DependencyCacheStatus(StrEnum):
    """How FastAPI resolved a logical dependency occurrence."""

    HIT = "hit"
    MISS = "miss"
    BYPASS = "bypass"


class DependencyScope(StrEnum):
    """The teardown scope configured for a generator dependency."""

    FUNCTION = "function"
    REQUEST = "request"


@dataclass(slots=True, frozen=True)
class TraceError:
    """Redacted structured error information captured during tracing."""

    type: str
    message: str
    stack: str | None = None


@dataclass(slots=True, frozen=True)
class Diagnostic:
    """A diagnostic finding associated with a trace or segment."""

    code: str
    severity: str
    message: str
    segment_id: str | None = None


def _validate_logical_dependency(
    *,
    cache_status: DependencyCacheStatus,
    cached_from_id: str | None,
    setup_segment_id: str | None,
    cleanup_segment_id: str | None,
) -> None:
    if cache_status is DependencyCacheStatus.HIT:
        if cached_from_id is None:
            raise ValueError("A cache hit must reference its source dependency.")
        if setup_segment_id is not None or cleanup_segment_id is not None:
            raise ValueError("A cache hit cannot reference execution segments.")
    elif cached_from_id is not None:
        raise ValueError("Only a cache hit can reference a source dependency.")
    if cleanup_segment_id is not None and setup_segment_id is None:
        raise ValueError("A cleanup segment requires a setup segment.")


@dataclass(slots=True)
class LogicalDependencyNode:
    """A logical dependency resolution occurrence within an active trace."""

    id: str
    trace_id: str
    name: str
    cache_status: DependencyCacheStatus
    parent_id: str | None = None
    cached_from_id: str | None = None
    setup_segment_id: str | None = None
    cleanup_segment_id: str | None = None
    scope: DependencyScope | None = None

    def __post_init__(self) -> None:
        self._validate()

    @property
    def use_cache(self) -> bool:
        """Return whether FastAPI caching is enabled for this occurrence."""
        return self.cache_status is not DependencyCacheStatus.BYPASS

    @property
    def executed(self) -> bool:
        """Return whether the dependency callable executed for this occurrence."""
        return self.cache_status is not DependencyCacheStatus.HIT

    @property
    def execution_segment_ids(self) -> tuple[str, ...]:
        """Return setup and cleanup segment IDs in lifecycle order."""
        return tuple(
            segment_id
            for segment_id in (self.setup_segment_id, self.cleanup_segment_id)
            if segment_id is not None
        )

    def snapshot(self) -> "LogicalDependencySnapshot":
        """Return an immutable copy after validating the current node state."""
        self._validate()
        return LogicalDependencySnapshot(
            id=self.id,
            trace_id=self.trace_id,
            name=self.name,
            cache_status=self.cache_status,
            parent_id=self.parent_id,
            cached_from_id=self.cached_from_id,
            setup_segment_id=self.setup_segment_id,
            cleanup_segment_id=self.cleanup_segment_id,
            scope=self.scope,
        )

    def _validate(self) -> None:
        _validate_logical_dependency(
            cache_status=self.cache_status,
            cached_from_id=self.cached_from_id,
            setup_segment_id=self.setup_segment_id,
            cleanup_segment_id=self.cleanup_segment_id,
        )


@dataclass(slots=True)
class TraceSegment:
    """A mutable measured operation owned by an in-flight request trace."""

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
        """Return the measured wall-clock duration without clamping."""
        if self.end_ns is None:
            return None
        return (self.end_ns - self.start_ns) / 1_000_000

    def snapshot(self) -> "TraceSegmentSnapshot":
        """Return a deeply immutable copy of the current segment state."""
        return TraceSegmentSnapshot(
            id=self.id,
            trace_id=self.trace_id,
            type=self.type,
            name=self.name,
            start_ns=self.start_ns,
            end_ns=self.end_ns,
            parent_id=self.parent_id,
            logical_dependency_id=self.logical_dependency_id,
            status=self.status,
            attributes=tuple(
                (key, freeze_json_value(value))
                for key, value in sorted(self.attributes.items())
            ),
            error=self.error,
        )


@dataclass(slots=True)
class RequestTrace:
    """Mutable request lifecycle data owned by an active collector."""

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
    logical_dependencies: list[LogicalDependencyNode] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    error: TraceError | None = None
    complete: bool = False

    @property
    def time_to_response_start_ms(self) -> float | None:
        """Return elapsed time before the response start event."""
        if self.response_started_ns is None:
            return None
        return (self.response_started_ns - self.request_received_ns) / 1_000_000

    @property
    def response_complete_duration_ms(self) -> float | None:
        """Return elapsed time through the final response body event."""
        if self.response_body_completed_ns is None:
            return None
        return (self.response_body_completed_ns - self.request_received_ns) / 1_000_000

    @property
    def response_send_duration_ms(self) -> float | None:
        """Return elapsed time between response start and body completion."""
        if self.response_started_ns is None or self.response_body_completed_ns is None:
            return None
        return (self.response_body_completed_ns - self.response_started_ns) / 1_000_000

    @property
    def post_response_duration_ms(self) -> float | None:
        """Return application work observed after response body completion."""
        if (
            self.response_body_completed_ns is None
            or self.application_completed_ns is None
        ):
            return None
        return (
            self.application_completed_ns - self.response_body_completed_ns
        ) / 1_000_000

    @property
    def application_duration_ms(self) -> float | None:
        """Return elapsed time until the downstream application returns."""
        if self.application_completed_ns is None:
            return None
        return (self.application_completed_ns - self.request_received_ns) / 1_000_000

    def snapshot(self) -> "RequestTraceSnapshot":
        """Return a deeply immutable copy of the current request trace state."""
        return RequestTraceSnapshot(
            schema_version=self.schema_version,
            id=self.id,
            method=self.method,
            path=self.path,
            route=self.route,
            started_at=self.started_at,
            request_received_ns=self.request_received_ns,
            response_started_ns=self.response_started_ns,
            response_body_completed_ns=self.response_body_completed_ns,
            application_completed_ns=self.application_completed_ns,
            status_code=self.status_code,
            segments=tuple(segment.snapshot() for segment in self.segments),
            logical_dependencies=tuple(
                dependency.snapshot() for dependency in self.logical_dependencies
            ),
            diagnostics=tuple(self.diagnostics),
            error=self.error,
            complete=self.complete,
        )


@dataclass(slots=True, frozen=True)
class TraceSegmentSnapshot:
    """A deeply immutable trace segment safe for storage and export."""

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

    @property
    def duration_ms(self) -> float | None:
        """Return the measured wall-clock duration without clamping."""
        if self.end_ns is None:
            return None
        return (self.end_ns - self.start_ns) / 1_000_000


@dataclass(slots=True, frozen=True)
class LogicalDependencySnapshot:
    """An immutable logical dependency occurrence safe for storage."""

    id: str
    trace_id: str
    name: str
    cache_status: DependencyCacheStatus
    parent_id: str | None
    cached_from_id: str | None
    setup_segment_id: str | None
    cleanup_segment_id: str | None
    scope: DependencyScope | None

    def __post_init__(self) -> None:
        _validate_logical_dependency(
            cache_status=self.cache_status,
            cached_from_id=self.cached_from_id,
            setup_segment_id=self.setup_segment_id,
            cleanup_segment_id=self.cleanup_segment_id,
        )

    @property
    def use_cache(self) -> bool:
        """Return whether FastAPI caching is enabled for this occurrence."""
        return self.cache_status is not DependencyCacheStatus.BYPASS

    @property
    def executed(self) -> bool:
        """Return whether the dependency callable executed for this occurrence."""
        return self.cache_status is not DependencyCacheStatus.HIT

    @property
    def execution_segment_ids(self) -> tuple[str, ...]:
        """Return setup and cleanup segment IDs in lifecycle order."""
        return tuple(
            segment_id
            for segment_id in (self.setup_segment_id, self.cleanup_segment_id)
            if segment_id is not None
        )


@dataclass(slots=True, frozen=True)
class RequestTraceSnapshot:
    """A deeply immutable request trace safe for storage and export."""

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
    logical_dependencies: tuple[LogicalDependencySnapshot, ...]
    diagnostics: tuple[Diagnostic, ...]
    error: TraceError | None
    complete: bool

    @property
    def time_to_response_start_ms(self) -> float | None:
        """Return elapsed time before the response start event."""
        if self.response_started_ns is None:
            return None
        return (self.response_started_ns - self.request_received_ns) / 1_000_000

    @property
    def response_complete_duration_ms(self) -> float | None:
        """Return elapsed time through the final response body event."""
        if self.response_body_completed_ns is None:
            return None
        return (self.response_body_completed_ns - self.request_received_ns) / 1_000_000

    @property
    def response_send_duration_ms(self) -> float | None:
        """Return elapsed time between response start and body completion."""
        if self.response_started_ns is None or self.response_body_completed_ns is None:
            return None
        return (self.response_body_completed_ns - self.response_started_ns) / 1_000_000

    @property
    def post_response_duration_ms(self) -> float | None:
        """Return application work observed after response body completion."""
        if (
            self.response_body_completed_ns is None
            or self.application_completed_ns is None
        ):
            return None
        return (
            self.application_completed_ns - self.response_body_completed_ns
        ) / 1_000_000

    @property
    def application_duration_ms(self) -> float | None:
        """Return elapsed time until the downstream application returns."""
        if self.application_completed_ns is None:
            return None
        return (self.application_completed_ns - self.request_received_ns) / 1_000_000

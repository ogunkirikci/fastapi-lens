"""Deterministic, versioned, and byte-bounded JSON trace export."""

import json
from datetime import UTC, datetime
from typing import Final

from fastapi_lens.models import (
    Diagnostic,
    FrozenJsonArray,
    FrozenJsonObject,
    FrozenJsonValue,
    JsonValue,
    LogicalDependencySnapshot,
    RequestTraceSnapshot,
    TraceError,
    TraceSegmentSnapshot,
)

SUPPORTED_SCHEMA_VERSION: Final = "1.0"
DEFAULT_MAX_TRACE_BYTES: Final = 1_000_000


class UnsupportedSchemaVersionError(ValueError):
    """Raised when no JSON exporter exists for a snapshot schema version."""


class TraceJsonSizeError(ValueError):
    """Raised when serialized trace JSON exceeds its configured byte limit."""

    def __init__(self, *, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"Serialized trace requires {actual_bytes} bytes; limit is {max_bytes} bytes."
        )


def _json_datetime(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("Trace started_at must be timezone-aware.")
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _thaw_json_value(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, FrozenJsonArray):
        return [_thaw_json_value(item) for item in value]
    if isinstance(value, FrozenJsonObject):
        return {key: _thaw_json_value(item) for key, item in value}
    return value


def _error_to_dict(error: TraceError | None) -> dict[str, JsonValue] | None:
    if error is None:
        return None
    return {
        "type": error.type,
        "message": error.message,
        "stack": error.stack,
    }


def _diagnostic_to_dict(diagnostic: Diagnostic) -> dict[str, JsonValue]:
    return {
        "code": diagnostic.code,
        "severity": diagnostic.severity,
        "message": diagnostic.message,
        "segment_id": diagnostic.segment_id,
    }


def _segment_to_dict(segment: TraceSegmentSnapshot) -> dict[str, JsonValue]:
    return {
        "id": segment.id,
        "trace_id": segment.trace_id,
        "type": segment.type.value,
        "name": segment.name,
        "start_ns": segment.start_ns,
        "end_ns": segment.end_ns,
        "duration_ms": segment.duration_ms,
        "parent_id": segment.parent_id,
        "logical_dependency_id": segment.logical_dependency_id,
        "status": segment.status.value,
        "attributes": {
            key: _thaw_json_value(value) for key, value in segment.attributes
        },
        "error": _error_to_dict(segment.error),
    }


def _dependency_to_dict(
    dependency: LogicalDependencySnapshot,
) -> dict[str, JsonValue]:
    return {
        "id": dependency.id,
        "trace_id": dependency.trace_id,
        "name": dependency.name,
        "cache_status": dependency.cache_status.value,
        "use_cache": dependency.use_cache,
        "executed": dependency.executed,
        "parent_id": dependency.parent_id,
        "cached_from_id": dependency.cached_from_id,
        "setup_segment_id": dependency.setup_segment_id,
        "cleanup_segment_id": dependency.cleanup_segment_id,
        "scope": dependency.scope.value if dependency.scope is not None else None,
    }


def trace_snapshot_to_dict(trace: RequestTraceSnapshot) -> dict[str, JsonValue]:
    """Convert a supported immutable snapshot into its public JSON object."""
    if trace.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"Unsupported trace schema version: {trace.schema_version}."
        )
    return {
        "schema_version": trace.schema_version,
        "trace_id": trace.id,
        "method": trace.method,
        "route": trace.route,
        "path": trace.path,
        "status_code": trace.status_code,
        "started_at": _json_datetime(trace.started_at),
        "request_received_ns": trace.request_received_ns,
        "response_started_ns": trace.response_started_ns,
        "response_body_completed_ns": trace.response_body_completed_ns,
        "application_completed_ns": trace.application_completed_ns,
        "time_to_response_start_ms": trace.time_to_response_start_ms,
        "response_complete_duration_ms": trace.response_complete_duration_ms,
        "response_send_duration_ms": trace.response_send_duration_ms,
        "post_response_duration_ms": trace.post_response_duration_ms,
        "application_duration_ms": trace.application_duration_ms,
        "complete": trace.complete,
        "segments": [_segment_to_dict(segment) for segment in trace.segments],
        "logical_dependencies": [
            _dependency_to_dict(dependency) for dependency in trace.logical_dependencies
        ],
        "diagnostics": [
            _diagnostic_to_dict(diagnostic) for diagnostic in trace.diagnostics
        ],
        "error": _error_to_dict(trace.error),
    }


def trace_snapshot_to_json(
    trace: RequestTraceSnapshot,
    *,
    max_bytes: int = DEFAULT_MAX_TRACE_BYTES,
) -> bytes:
    """Serialize a trace to deterministic UTF-8 JSON within a hard byte limit."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero.")
    payload = json.dumps(
        trace_snapshot_to_dict(trace),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > max_bytes:
        raise TraceJsonSizeError(
            actual_bytes=len(payload),
            max_bytes=max_bytes,
        )
    return payload

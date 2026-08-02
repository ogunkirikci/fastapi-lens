import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from fastapi_lens.exporters.json import (
    TraceJsonSizeError,
    UnsupportedSchemaVersionError,
    trace_snapshot_to_dict,
    trace_snapshot_to_json,
)
from fastapi_lens.models import (
    DependencyCacheStatus,
    DependencyScope,
    Diagnostic,
    JsonValue,
    LogicalDependencyNode,
    RequestTrace,
    SegmentStatus,
    SegmentType,
    TraceError,
    TraceSegment,
)


def make_trace() -> RequestTrace:
    attributes: dict[str, JsonValue] = {
        "empty_array": [],
        "empty_object": {},
        "metadata": {"attempts": [1, 2]},
    }
    segment_error = TraceError(
        type="ValueError",
        message="Segment failed",
        stack="stack line",
    )
    segment = TraceSegment(
        id="segment-1",
        trace_id="trace-1",
        type=SegmentType.DEPENDENCY_SETUP,
        name="authenticate_user",
        start_ns=2_000_000,
        end_ns=3_000_000,
        logical_dependency_id="dependency-1",
        status=SegmentStatus.ERROR,
        attributes=attributes,
        error=segment_error,
    )
    dependency = LogicalDependencyNode(
        id="dependency-1",
        trace_id="trace-1",
        name="authenticate_user",
        cache_status=DependencyCacheStatus.MISS,
        setup_segment_id="segment-1",
        scope=DependencyScope.REQUEST,
    )
    return RequestTrace(
        schema_version="1.0",
        id="trace-1",
        method="GET",
        route="/items/{item_id}",
        path="/items/42",
        started_at=datetime(2026, 8, 2, 20, 0, tzinfo=UTC),
        request_received_ns=1_000_000,
        response_started_ns=4_000_000,
        response_body_completed_ns=6_000_000,
        application_completed_ns=7_000_000,
        status_code=500,
        segments=[segment],
        logical_dependencies=[dependency],
        diagnostics=[
            Diagnostic(
                code="DEPENDENCY_ERROR",
                severity="error",
                message="Dependency execution failed.",
                segment_id="segment-1",
            )
        ],
        error=TraceError(type="RuntimeError", message="Request failed"),
        complete=True,
    )


def test_json_object_contains_versioned_lifecycle_and_trace_data() -> None:
    exported = trace_snapshot_to_dict(make_trace().snapshot())

    assert exported["schema_version"] == "1.0"
    assert exported["trace_id"] == "trace-1"
    assert exported["started_at"] == "2026-08-02T20:00:00.000Z"
    assert exported["time_to_response_start_ms"] == 3.0
    assert exported["response_complete_duration_ms"] == 5.0
    assert exported["response_send_duration_ms"] == 2.0
    assert exported["post_response_duration_ms"] == 1.0
    assert exported["application_duration_ms"] == 6.0
    assert exported["complete"] is True
    assert exported["error"] == {
        "type": "RuntimeError",
        "message": "Request failed",
        "stack": None,
    }


def test_json_object_exports_segments_dependencies_and_diagnostics() -> None:
    exported = trace_snapshot_to_dict(make_trace().snapshot())

    segments = exported["segments"]
    dependencies = exported["logical_dependencies"]
    diagnostics = exported["diagnostics"]

    assert isinstance(segments, list)
    assert isinstance(dependencies, list)
    assert isinstance(diagnostics, list)
    segment = segments[0]
    dependency = dependencies[0]
    diagnostic = diagnostics[0]
    assert isinstance(segment, dict)
    assert isinstance(dependency, dict)
    assert isinstance(diagnostic, dict)
    assert segment["duration_ms"] == 1.0
    assert segment["attributes"] == {
        "empty_array": [],
        "empty_object": {},
        "metadata": {"attempts": [1, 2]},
    }
    segment_error = segment["error"]
    assert isinstance(segment_error, dict)
    assert segment_error["type"] == "ValueError"
    assert dependency == {
        "id": "dependency-1",
        "trace_id": "trace-1",
        "name": "authenticate_user",
        "cache_status": "miss",
        "use_cache": True,
        "executed": True,
        "parent_id": None,
        "cached_from_id": None,
        "setup_segment_id": "segment-1",
        "cleanup_segment_id": None,
        "scope": "request",
    }
    assert diagnostic["segment_id"] == "segment-1"


def test_json_object_preserves_absent_trace_and_segment_errors() -> None:
    trace = make_trace()
    trace.error = None
    trace.segments[0].error = None

    exported = trace_snapshot_to_dict(trace.snapshot())
    segments = exported["segments"]

    assert exported["error"] is None
    assert isinstance(segments, list)
    segment = segments[0]
    assert isinstance(segment, dict)
    assert segment["error"] is None


def test_json_bytes_are_deterministic_and_parseable() -> None:
    snapshot = make_trace().snapshot()

    first = trace_snapshot_to_json(snapshot)
    second = trace_snapshot_to_json(snapshot)

    assert first == second
    assert json.loads(first)["trace_id"] == "trace-1"


def test_started_at_is_normalized_to_utc() -> None:
    trace = make_trace()
    trace.started_at = datetime(
        2026,
        8,
        2,
        23,
        0,
        tzinfo=timezone(timedelta(hours=3)),
    )

    exported = trace_snapshot_to_dict(trace.snapshot())

    assert exported["started_at"] == "2026-08-02T20:00:00.000Z"


def test_naive_started_at_is_rejected() -> None:
    trace = make_trace()
    trace.started_at = datetime(2026, 8, 2, 20, 0)

    with pytest.raises(
        ValueError,
        match=r"Trace started_at must be timezone-aware\.",
    ):
        trace_snapshot_to_dict(trace.snapshot())


def test_unsupported_schema_version_is_rejected() -> None:
    trace = make_trace()
    trace.schema_version = "2.0"

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=r"Unsupported trace schema version: 2\.0\.",
    ):
        trace_snapshot_to_dict(trace.snapshot())


def test_json_respects_exact_byte_limit() -> None:
    snapshot = make_trace().snapshot()
    payload = trace_snapshot_to_json(snapshot)

    assert trace_snapshot_to_json(snapshot, max_bytes=len(payload)) == payload

    with pytest.raises(TraceJsonSizeError) as captured:
        trace_snapshot_to_json(snapshot, max_bytes=len(payload) - 1)

    assert captured.value.actual_bytes == len(payload)
    assert captured.value.max_bytes == len(payload) - 1
    assert str(captured.value) == (
        f"Serialized trace requires {len(payload)} bytes; "
        f"limit is {len(payload) - 1} bytes."
    )


@pytest.mark.parametrize("max_bytes", [0, -1])
def test_json_rejects_non_positive_byte_limit(max_bytes: int) -> None:
    with pytest.raises(
        ValueError,
        match=r"max_bytes must be greater than zero\.",
    ):
        trace_snapshot_to_json(make_trace().snapshot(), max_bytes=max_bytes)

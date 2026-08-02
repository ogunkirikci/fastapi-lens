from fastapi_lens.models import (
    SegmentStatus,
    SegmentType,
    TraceError,
    TraceSegment,
)


def make_segment(**overrides: object) -> TraceSegment:
    values: dict[str, object] = {
        "id": "segment-1",
        "trace_id": "trace-1",
        "type": SegmentType.HANDLER,
        "name": "get_items",
        "start_ns": 1_000_000,
    }
    values.update(overrides)
    return TraceSegment(**values)  # type: ignore[arg-type]


def test_new_segment_has_safe_incomplete_defaults() -> None:
    segment = make_segment()

    assert segment.end_ns is None
    assert segment.parent_id is None
    assert segment.logical_dependency_id is None
    assert segment.status is SegmentStatus.INCOMPLETE
    assert segment.attributes == {}
    assert segment.error is None
    assert segment.duration_ms is None


def test_segment_attributes_are_not_shared() -> None:
    first = make_segment(id="segment-1")
    second = make_segment(id="segment-2")

    first.attributes["cached"] = True

    assert first.attributes == {"cached": True}
    assert second.attributes == {}


def test_segment_duration_converts_nanoseconds_to_milliseconds() -> None:
    segment = make_segment(start_ns=2_000_000, end_ns=3_500_000)

    assert segment.duration_ms == 1.5


def test_segment_duration_preserves_an_invalid_negative_interval() -> None:
    segment = make_segment(start_ns=2_000_000, end_ns=1_500_000)

    assert segment.duration_ms == -0.5


def test_segment_records_relationship_status_and_error_metadata() -> None:
    error = TraceError(type="RuntimeError", message="Operation failed")
    segment = make_segment(
        type=SegmentType.DEPENDENCY_CLEANUP,
        parent_id="segment-parent",
        logical_dependency_id="dependency-1",
        status=SegmentStatus.ERROR,
        error=error,
    )

    assert segment.type is SegmentType.DEPENDENCY_CLEANUP
    assert segment.parent_id == "segment-parent"
    assert segment.logical_dependency_id == "dependency-1"
    assert segment.status is SegmentStatus.ERROR
    assert segment.error is error

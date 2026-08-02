from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from fastapi_lens.models import (
    Diagnostic,
    JsonValue,
    RequestTrace,
    SegmentStatus,
    SegmentType,
    TraceError,
    TraceSegment,
)


def make_completed_trace() -> RequestTrace:
    items: list[JsonValue] = [1, {"ok": True}]
    attributes: dict[str, JsonValue] = {
        "z_last": None,
        "metadata": {"items": items},
    }
    segment = TraceSegment(
        id="segment-1",
        trace_id="trace-1",
        type=SegmentType.HANDLER,
        name="get_items",
        start_ns=2_000_000,
        end_ns=4_000_000,
        status=SegmentStatus.OK,
        attributes=attributes,
    )
    return RequestTrace(
        schema_version="1.0",
        id="trace-1",
        method="GET",
        path="/items/42",
        route="/items/{item_id}",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_received_ns=1_000_000,
        response_started_ns=3_000_000,
        response_body_completed_ns=5_000_000,
        application_completed_ns=7_000_000,
        status_code=200,
        segments=[segment],
        diagnostics=[
            Diagnostic(
                code="slow_request",
                severity="warning",
                message="Request exceeded the configured threshold.",
            )
        ],
        complete=True,
    )


def test_segment_snapshot_deeply_freezes_attributes_in_stable_key_order() -> None:
    segment = make_completed_trace().segments[0]

    snapshot = segment.snapshot()

    assert snapshot.attributes == (
        ("metadata", (("items", (1, (("ok", True),))),)),
        ("z_last", None),
    )
    assert snapshot.duration_ms == 2.0


def test_segment_snapshot_keeps_missing_duration_unknown() -> None:
    segment = make_completed_trace().segments[0]
    segment.end_ns = None

    assert segment.snapshot().duration_ms is None


def test_request_snapshot_copies_all_state_and_lifecycle_properties() -> None:
    trace = make_completed_trace()

    snapshot = trace.snapshot()

    assert snapshot.schema_version == "1.0"
    assert snapshot.id == "trace-1"
    assert snapshot.method == "GET"
    assert snapshot.path == "/items/42"
    assert snapshot.route == "/items/{item_id}"
    assert snapshot.status_code == 200
    assert snapshot.complete is True
    assert len(snapshot.segments) == 1
    assert len(snapshot.diagnostics) == 1
    assert snapshot.time_to_response_start_ms == 2.0
    assert snapshot.response_complete_duration_ms == 4.0
    assert snapshot.response_send_duration_ms == 2.0
    assert snapshot.post_response_duration_ms == 2.0
    assert snapshot.application_duration_ms == 6.0


def test_request_snapshot_lifecycle_properties_preserve_missing_checkpoints() -> None:
    trace = make_completed_trace()
    trace.response_started_ns = None
    trace.response_body_completed_ns = None
    trace.application_completed_ns = None

    snapshot = trace.snapshot()

    assert snapshot.time_to_response_start_ms is None
    assert snapshot.response_complete_duration_ms is None
    assert snapshot.response_send_duration_ms is None
    assert snapshot.post_response_duration_ms is None
    assert snapshot.application_duration_ms is None


def test_snapshot_is_detached_from_later_source_mutations() -> None:
    trace = make_completed_trace()
    segment = trace.segments[0]
    snapshot = trace.snapshot()

    trace.path = "/changed"
    trace.segments.clear()
    trace.diagnostics.clear()
    segment.name = "changed"
    segment.attributes.clear()

    assert snapshot.path == "/items/42"
    assert snapshot.segments[0].name == "get_items"
    assert snapshot.segments[0].attributes == (
        ("metadata", (("items", (1, (("ok", True),))),)),
        ("z_last", None),
    )
    assert snapshot.diagnostics[0].code == "slow_request"


def test_snapshot_and_nested_segment_are_frozen() -> None:
    snapshot = make_completed_trace().snapshot()

    with pytest.raises(FrozenInstanceError):
        snapshot.path = "/changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.segments[0].name = "changed"  # type: ignore[misc]


def test_snapshot_retains_immutable_error_models() -> None:
    trace = make_completed_trace()
    trace_error = TraceError(type="RuntimeError", message="Request failed")
    segment_error = TraceError(type="ValueError", message="Segment failed")
    trace.error = trace_error
    trace.segments[0].error = segment_error

    snapshot = trace.snapshot()

    assert snapshot.error is trace_error
    assert snapshot.segments[0].error is segment_error

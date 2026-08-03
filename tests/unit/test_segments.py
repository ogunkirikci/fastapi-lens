import asyncio
import inspect

import pytest

from fastapi_latensight import current_trace, trace_segment
from fastapi_latensight.context import bind_request_context, reset_request_context
from fastapi_latensight.models import SegmentStatus

from .test_context import make_collector


def test_sync_segment_records_attributes_and_success() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)

    with current_trace.segment_sync(
        "calculate_price",
        attributes={"currency": "USD"},
    ) as segment:
        assert segment is not None
        assert segment.name == "calculate_price"

    reset_request_context(context_token)
    snapshot = collector.finalize()
    recorded = snapshot.segments[0]
    assert recorded.status is SegmentStatus.OK
    assert recorded.duration_ms is not None
    assert recorded.duration_ms >= 0
    assert recorded.attributes == (("currency", "USD"),)


async def test_async_segment_records_success() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)

    async with current_trace.segment("external_operation") as segment:
        assert segment is not None
        await asyncio.sleep(0)

    reset_request_context(context_token)
    recorded = collector.finalize().segments[0]
    assert recorded.status is SegmentStatus.OK
    assert recorded.duration_ms is not None


def test_nested_custom_segments_preserve_parent_relationships() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)

    with (
        current_trace.segment_sync("outer") as outer,
        current_trace.segment_sync("inner") as inner,
    ):
        assert outer is not None
        assert inner is not None
        assert inner.parent_id == outer.id

    reset_request_context(context_token)
    snapshot = collector.finalize()
    assert snapshot.segments[1].parent_id == snapshot.segments[0].id


def test_sync_segment_records_error_without_changing_propagation() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)

    with (
        pytest.raises(ValueError, match="expected failure"),
        current_trace.segment_sync("failing"),
    ):
        raise ValueError("expected failure")

    reset_request_context(context_token)
    recorded = collector.finalize().segments[0]
    assert recorded.status is SegmentStatus.ERROR
    assert recorded.error is not None
    assert recorded.error.type == "ValueError"
    assert recorded.error.message == "expected failure"


async def test_async_segment_records_cancellation_without_changing_propagation() -> (
    None
):
    collector = make_collector()
    context_token = bind_request_context(collector)

    with pytest.raises(asyncio.CancelledError):
        async with current_trace.segment("cancelled"):
            raise asyncio.CancelledError

    reset_request_context(context_token)
    recorded = collector.finalize().segments[0]
    assert recorded.status is SegmentStatus.CANCELLED
    assert recorded.error is not None
    assert recorded.error.type == "CancelledError"


def test_context_managers_are_no_ops_without_an_active_trace() -> None:
    with current_trace.segment_sync("sync") as sync_segment:
        assert sync_segment is None


async def test_async_context_manager_is_no_op_without_an_active_trace() -> None:
    async with current_trace.segment("async") as async_segment:
        assert async_segment is None


def test_sync_decorator_preserves_signature_metadata_and_result() -> None:
    @trace_segment("decorated_sync")
    def add(left: int, right: int = 1) -> int:
        """Add two values."""
        return left + right

    collector = make_collector()
    context_token = bind_request_context(collector)
    result = add(2, right=3)
    reset_request_context(context_token)

    assert result == 5
    assert add.__name__ == "add"
    assert add.__doc__ == "Add two values."
    assert str(inspect.signature(add)) == "(left: int, right: int = 1) -> int"
    assert collector.finalize().segments[0].name == "decorated_sync"


async def test_async_decorator_preserves_signature_and_result() -> None:
    @trace_segment("decorated_async")
    async def add(left: int, right: int) -> int:
        return left + right

    collector = make_collector()
    context_token = bind_request_context(collector)
    result = await add(2, 3)
    reset_request_context(context_token)

    assert result == 5
    assert str(inspect.signature(add)) == "(left: int, right: int) -> int"
    assert collector.finalize().segments[0].name == "decorated_async"


def test_segment_started_after_finalization_is_rejected() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)
    snapshot = collector.finalize()

    with current_trace.segment_sync("late") as segment:
        assert segment is None

    reset_request_context(context_token)
    assert snapshot.segments == ()
    assert collector.late_segment_count == 1


def test_segment_finish_after_finalization_cannot_mutate_snapshot() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)

    with current_trace.segment_sync("active"):
        snapshot = collector.finalize()

    reset_request_context(context_token)
    assert snapshot.segments[0].status is SegmentStatus.INCOMPLETE
    assert snapshot.segments[0].end_ns is None
    assert collector.late_segment_count == 1

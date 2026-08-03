import asyncio
from datetime import UTC, datetime

from fastapi_latensight.collector import TraceCollector
from fastapi_latensight.context import (
    bind_request_context,
    current_collector,
    current_parent_segment_id,
    current_segment_stack,
    enter_segment,
    exit_segment,
    reset_request_context,
)
from fastapi_latensight.models import (
    DependencyCacheStatus,
    LogicalDependencyNode,
    RequestTrace,
    SegmentType,
    TraceSegment,
)


def make_collector(trace_id: str = "trace-1") -> TraceCollector:
    return TraceCollector(
        RequestTrace(
            schema_version="1.0",
            id=trace_id,
            method="GET",
            path="/items",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            request_received_ns=1_000_000,
        )
    )


def make_segment(
    segment_id: str,
    *,
    trace_id: str = "trace-1",
    parent_id: str | None = None,
) -> TraceSegment:
    return TraceSegment(
        id=segment_id,
        trace_id=trace_id,
        type=SegmentType.CUSTOM,
        name=segment_id,
        start_ns=2_000_000,
        parent_id=parent_id,
    )


def test_context_binding_and_reset_restore_previous_values() -> None:
    outer = make_collector("trace-outer")
    inner = make_collector("trace-inner")
    outer_token = bind_request_context(outer)
    outer_segment_token = enter_segment(
        make_segment("outer-segment", trace_id="trace-outer")
    )
    inner_token = bind_request_context(inner)

    assert current_collector() is inner
    assert current_segment_stack() == ()

    reset_request_context(inner_token)
    assert current_collector() is outer
    assert current_segment_stack() == ("outer-segment",)

    exit_segment(outer_segment_token)
    reset_request_context(outer_token)
    assert current_collector() is None
    assert current_segment_stack() == ()


def test_segment_recording_is_a_no_op_without_an_active_collector() -> None:
    segment = make_segment("segment-1")

    token = enter_segment(segment)
    exit_segment(token)

    assert token is None
    assert segment.parent_id is None
    assert current_segment_stack() == ()


def test_nested_segments_receive_parent_relationships_from_the_stack() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)
    root_token = enter_segment(make_segment("root"))
    child = make_segment("child")
    child_token = enter_segment(child)

    assert current_parent_segment_id() == "child"
    assert current_segment_stack() == ("root", "child")
    assert child.parent_id == "root"

    exit_segment(child_token)
    assert current_parent_segment_id() == "root"
    exit_segment(root_token)
    assert current_parent_segment_id() is None
    reset_request_context(context_token)


def test_explicit_parent_relationship_is_preserved() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)
    root_token = enter_segment(make_segment("root"))
    child = make_segment("child", parent_id="explicit-parent")
    child_token = enter_segment(child)

    assert child.parent_id == "explicit-parent"

    exit_segment(child_token)
    exit_segment(root_token)
    reset_request_context(context_token)


async def test_child_tasks_receive_independent_immutable_stack_copies() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)
    root_token = enter_segment(make_segment("root"))
    ready = asyncio.Event()
    observed: dict[str, tuple[str, ...]] = {}

    async def worker(segment_id: str) -> None:
        token = enter_segment(make_segment(segment_id))
        observed[segment_id] = current_segment_stack()
        ready.set()
        await asyncio.sleep(0)
        assert current_segment_stack() == ("root", segment_id)
        exit_segment(token)

    first = asyncio.create_task(worker("child-1"))
    await ready.wait()
    second = asyncio.create_task(worker("child-2"))
    await asyncio.gather(first, second)

    assert observed == {
        "child-1": ("root", "child-1"),
        "child-2": ("root", "child-2"),
    }
    assert current_segment_stack() == ("root",)

    exit_segment(root_token)
    reset_request_context(context_token)


async def test_context_and_parent_stack_propagate_to_worker_threads() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)
    root_token = enter_segment(make_segment("root"))

    def record_child() -> tuple[TraceCollector | None, tuple[str, ...]]:
        observed_collector = current_collector()
        child_token = enter_segment(make_segment("thread-child"))
        observed_stack = current_segment_stack()
        exit_segment(child_token)
        return observed_collector, observed_stack

    observed_collector, observed_stack = await asyncio.to_thread(record_child)

    assert observed_collector is collector
    assert observed_stack == ("root", "thread-child")
    assert current_segment_stack() == ("root",)

    exit_segment(root_token)
    reset_request_context(context_token)
    snapshot = collector.finalize()
    assert snapshot.segments[1].parent_id == "root"


async def test_concurrent_thread_writes_are_collected_without_loss() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)
    root_token = enter_segment(make_segment("root"))

    def record_child(index: int) -> None:
        token = enter_segment(make_segment(f"child-{index}"))
        exit_segment(token)

    await asyncio.gather(
        *(asyncio.to_thread(record_child, index) for index in range(50))
    )

    exit_segment(root_token)
    reset_request_context(context_token)
    snapshot = collector.finalize()
    assert len(snapshot.segments) == 51
    assert {segment.parent_id for segment in snapshot.segments[1:]} == {"root"}


def test_finalization_is_stable_and_rejects_late_segments() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)
    first_token = enter_segment(make_segment("first"))
    exit_segment(first_token)

    first_snapshot = collector.finalize()
    collector.trace.path = "/changed"
    late_token = enter_segment(make_segment("late"))
    second_snapshot = collector.finalize()

    assert collector.finalized is True
    assert collector.late_segment_count == 1
    assert late_token is None
    assert first_snapshot is second_snapshot
    assert first_snapshot.path == "/items"
    assert [segment.id for segment in first_snapshot.segments] == ["first"]
    assert current_segment_stack() == ()

    reset_request_context(context_token)


def test_collector_rejects_logical_dependencies_after_finalization() -> None:
    collector = make_collector()
    dependency = LogicalDependencyNode(
        id="dependency-1",
        trace_id="trace-1",
        name="dependency",
        cache_status=DependencyCacheStatus.MISS,
    )

    assert collector.add_logical_dependency(dependency) is True
    snapshot = collector.finalize()
    assert collector.add_logical_dependency(dependency) is False

    assert snapshot.logical_dependencies[0].id == "dependency-1"
    assert collector.late_segment_count == 1

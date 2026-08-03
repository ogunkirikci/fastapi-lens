from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from fastapi_latensight.models import (
    DependencyCacheStatus,
    DependencyScope,
    LogicalDependencyNode,
    RequestTrace,
)


def make_node(
    *,
    node_id: str = "dependency-1",
    cache_status: DependencyCacheStatus = DependencyCacheStatus.MISS,
    parent_id: str | None = None,
    cached_from_id: str | None = None,
    setup_segment_id: str | None = None,
    cleanup_segment_id: str | None = None,
    scope: DependencyScope | None = None,
) -> LogicalDependencyNode:
    return LogicalDependencyNode(
        id=node_id,
        trace_id="trace-1",
        name="authenticate_user",
        cache_status=cache_status,
        parent_id=parent_id,
        cached_from_id=cached_from_id,
        setup_segment_id=setup_segment_id,
        cleanup_segment_id=cleanup_segment_id,
        scope=scope,
    )


def test_cache_miss_records_setup_and_cleanup_under_one_node() -> None:
    node = make_node(
        parent_id="dependency-parent",
        setup_segment_id="segment-setup",
        cleanup_segment_id="segment-cleanup",
        scope=DependencyScope.REQUEST,
    )

    assert node.use_cache is True
    assert node.executed is True
    assert node.parent_id == "dependency-parent"
    assert node.scope is DependencyScope.REQUEST
    assert node.execution_segment_ids == ("segment-setup", "segment-cleanup")


def test_cache_hit_references_source_without_fake_execution_segments() -> None:
    node = make_node(
        node_id="dependency-2",
        cache_status=DependencyCacheStatus.HIT,
        cached_from_id="dependency-1",
    )

    assert node.use_cache is True
    assert node.executed is False
    assert node.cached_from_id == "dependency-1"
    assert node.execution_segment_ids == ()


def test_cache_bypass_represents_use_cache_false_execution() -> None:
    node = make_node(
        cache_status=DependencyCacheStatus.BYPASS,
        setup_segment_id="segment-setup",
        scope=DependencyScope.FUNCTION,
    )

    assert node.use_cache is False
    assert node.executed is True
    assert node.execution_segment_ids == ("segment-setup",)


def test_cache_hit_requires_a_source_dependency() -> None:
    with pytest.raises(
        ValueError,
        match=r"A cache hit must reference its source dependency\.",
    ):
        make_node(cache_status=DependencyCacheStatus.HIT)


@pytest.mark.parametrize(
    ("setup_segment_id", "cleanup_segment_id"),
    [("segment-setup", None), (None, "segment-cleanup")],
)
def test_cache_hit_rejects_execution_segments(
    setup_segment_id: str | None,
    cleanup_segment_id: str | None,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"A cache hit cannot reference execution segments\.",
    ):
        make_node(
            cache_status=DependencyCacheStatus.HIT,
            cached_from_id="dependency-1",
            setup_segment_id=setup_segment_id,
            cleanup_segment_id=cleanup_segment_id,
        )


@pytest.mark.parametrize(
    "cache_status",
    [DependencyCacheStatus.MISS, DependencyCacheStatus.BYPASS],
)
def test_executed_dependency_rejects_a_cache_source(
    cache_status: DependencyCacheStatus,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"Only a cache hit can reference a source dependency\.",
    ):
        make_node(
            cache_status=cache_status,
            cached_from_id="dependency-1",
        )


def test_cleanup_segment_requires_a_setup_segment() -> None:
    with pytest.raises(
        ValueError,
        match=r"A cleanup segment requires a setup segment\.",
    ):
        make_node(cleanup_segment_id="segment-cleanup")


def test_snapshot_revalidates_a_mutated_node() -> None:
    node = make_node()
    node.cached_from_id = "dependency-1"

    with pytest.raises(
        ValueError,
        match=r"Only a cache hit can reference a source dependency\.",
    ):
        node.snapshot()


def test_request_snapshot_detaches_and_freezes_logical_dependencies() -> None:
    node = make_node(setup_segment_id="segment-setup")
    trace = RequestTrace(
        schema_version="1.0",
        id="trace-1",
        method="GET",
        path="/items",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_received_ns=1_000_000,
        logical_dependencies=[node],
    )

    snapshot = trace.snapshot()
    node.name = "changed"
    trace.logical_dependencies.clear()

    dependency = snapshot.logical_dependencies[0]
    assert dependency.name == "authenticate_user"
    assert dependency.use_cache is True
    assert dependency.executed is True
    assert dependency.execution_segment_ids == ("segment-setup",)
    with pytest.raises(FrozenInstanceError):
        dependency.name = "changed"  # type: ignore[misc]

import asyncio
import re
from datetime import UTC, datetime

import pytest

from fastapi_latensight.exporters.json import TraceJsonSizeError, trace_snapshot_to_json
from fastapi_latensight.models import RequestTrace, RequestTraceSnapshot
from fastapi_latensight.storage.base import TraceStore
from fastapi_latensight.storage.memory import MemoryTraceStore


def make_snapshot(
    trace_id: str,
    *,
    application_completed_ns: int | None,
    path: str = "/items",
) -> RequestTraceSnapshot:
    return RequestTrace(
        schema_version="1.0",
        id=trace_id,
        method="GET",
        path=path,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_received_ns=1_000_000,
        application_completed_ns=application_completed_ns,
        complete=application_completed_ns is not None,
    ).snapshot()


def accepts_trace_store(store: TraceStore) -> TraceStore:
    return store


def test_memory_store_satisfies_trace_store_protocol_statically() -> None:
    store = MemoryTraceStore()

    assert accepts_trace_store(store) is store
    assert store.process_local is True


async def test_save_get_and_clear_snapshots() -> None:
    store = MemoryTraceStore()
    snapshot = make_snapshot("trace-1", application_completed_ns=10)

    await store.save(snapshot)

    assert await store.get("trace-1") is snapshot
    assert await store.get("missing") is None

    await store.clear()
    assert await store.get("trace-1") is None
    assert await store.list() == []


async def test_oldest_trace_is_evicted_by_completion_time_not_insertion() -> None:
    store = MemoryTraceStore(max_traces=2)
    newest = make_snapshot("trace-newest", application_completed_ns=30)
    oldest = make_snapshot("trace-oldest", application_completed_ns=10)
    middle = make_snapshot("trace-middle", application_completed_ns=20)

    await store.save(newest)
    await store.save(oldest)
    await store.save(middle)

    assert [trace.id for trace in await store.list()] == [
        "trace-newest",
        "trace-middle",
    ]
    assert await store.get("trace-oldest") is None


async def test_incomplete_trace_is_oldest_for_eviction() -> None:
    store = MemoryTraceStore(max_traces=2)

    await store.save(make_snapshot("trace-1", application_completed_ns=10))
    await store.save(make_snapshot("trace-incomplete", application_completed_ns=None))
    await store.save(make_snapshot("trace-2", application_completed_ns=20))

    assert [trace.id for trace in await store.list()] == ["trace-2", "trace-1"]


async def test_saving_an_existing_id_replaces_without_eviction() -> None:
    store = MemoryTraceStore(max_traces=2)
    original = make_snapshot("trace-1", application_completed_ns=10)
    replacement = make_snapshot(
        "trace-1",
        application_completed_ns=30,
        path="/updated",
    )
    other = make_snapshot("trace-2", application_completed_ns=20)

    await store.save(original)
    await store.save(other)
    await store.save(replacement)

    assert await store.get("trace-1") is replacement
    assert [trace.id for trace in await store.list()] == ["trace-1", "trace-2"]


async def test_list_order_is_stable_by_completion_time_then_trace_id() -> None:
    store = MemoryTraceStore()
    for trace_id in ("trace-a", "trace-c", "trace-b"):
        await store.save(make_snapshot(trace_id, application_completed_ns=10))

    assert [trace.id for trace in await store.list()] == [
        "trace-c",
        "trace-b",
        "trace-a",
    ]


async def test_list_applies_validated_pagination() -> None:
    store = MemoryTraceStore(max_page_size=3)
    for index in range(5):
        await store.save(
            make_snapshot(
                f"trace-{index}",
                application_completed_ns=index,
            )
        )

    page = await store.list(limit=2, offset=1)

    assert [trace.id for trace in page] == ["trace-3", "trace-2"]
    assert await store.list(limit=2, offset=10) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_traces": 0}, "max_traces must be greater than zero."),
        ({"max_page_size": 0}, "max_page_size must be greater than zero."),
        ({"max_trace_bytes": 0}, "max_trace_bytes must be greater than zero."),
    ],
)
def test_store_rejects_non_positive_limits(
    kwargs: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        MemoryTraceStore(**kwargs)


@pytest.mark.parametrize(
    ("limit", "offset", "message"),
    [
        (0, 0, "limit must be greater than zero."),
        (4, 0, "limit must not exceed max_page_size (3)."),
        (1, -1, "offset must be zero or greater."),
    ],
)
async def test_list_rejects_invalid_pagination(
    limit: int,
    offset: int,
    message: str,
) -> None:
    store = MemoryTraceStore(max_page_size=3)

    with pytest.raises(ValueError, match=re.escape(message)):
        await store.list(limit=limit, offset=offset)


async def test_store_rejects_mutable_trace_models() -> None:
    store = MemoryTraceStore()
    mutable_trace = RequestTrace(
        schema_version="1.0",
        id="trace-1",
        method="GET",
        path="/items",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_received_ns=1_000_000,
    )

    with pytest.raises(
        TypeError,
        match=r"MemoryTraceStore accepts RequestTraceSnapshot only\.",
    ):
        await store.save(mutable_trace)  # type: ignore[arg-type]


async def test_store_enforces_serialized_trace_byte_limit() -> None:
    snapshot = make_snapshot(
        "trace-1",
        application_completed_ns=10,
        path="/" + ("x" * 100),
    )
    payload = trace_snapshot_to_json(snapshot)
    store = MemoryTraceStore(max_trace_bytes=len(payload) - 1)

    with pytest.raises(TraceJsonSizeError):
        await store.save(snapshot)

    assert await store.get("trace-1") is None


async def test_concurrent_async_saves_are_bounded() -> None:
    store = MemoryTraceStore(max_traces=10)
    snapshots = [
        make_snapshot(f"trace-{index:02}", application_completed_ns=index)
        for index in range(50)
    ]

    await asyncio.gather(*(store.save(snapshot) for snapshot in snapshots))

    stored = await store.list(limit=10)
    assert [trace.application_completed_ns for trace in stored] == list(
        range(49, 39, -1)
    )


async def test_store_is_safe_across_worker_threads_and_event_loops() -> None:
    store = MemoryTraceStore(max_traces=20)
    snapshots = [
        make_snapshot(f"trace-{index:02}", application_completed_ns=index)
        for index in range(20)
    ]

    await asyncio.gather(
        *(
            asyncio.to_thread(asyncio.run, store.save(snapshot))
            for snapshot in snapshots
        )
    )

    stored = await store.list(limit=20)
    assert len(stored) == 20
    assert {trace.id for trace in stored} == {
        f"trace-{index:02}" for index in range(20)
    }

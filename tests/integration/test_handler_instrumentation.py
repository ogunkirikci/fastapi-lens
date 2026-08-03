import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast

import fastapi.routing
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from fastapi_latensight.collector import TraceCollector
from fastapi_latensight.context import (
    bind_request_context,
    current_collector,
    reset_request_context,
)
from fastapi_latensight.instrumentation.handler import (
    HandlerInstrumentation,
    handler_instrumentation,
)
from fastapi_latensight.middleware import LatensightMiddleware
from fastapi_latensight.models import (
    RequestTrace,
    RequestTraceSnapshot,
    SegmentStatus,
    SegmentType,
)
from fastapi_latensight.storage.memory import MemoryTraceStore


def make_collector() -> TraceCollector:
    return TraceCollector(
        RequestTrace(
            schema_version="1.0",
            id="trace-1",
            method="GET",
            path="/items",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            request_received_ns=1_000_000,
        )
    )


@contextmanager
def installed_handler_instrumentation() -> Iterator[None]:
    owner = object()
    handler_instrumentation.install(owner)
    try:
        yield
    finally:
        handler_instrumentation.remove(owner)


def stored_trace(store: MemoryTraceStore) -> RequestTraceSnapshot:
    return asyncio.run(store.list())[0]


def test_async_handler_records_wall_clock_segment() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    @app.get("/")
    async def async_endpoint() -> dict[str, str]:
        await asyncio.sleep(0)
        return {"status": "ok"}

    with (
        installed_handler_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        assert client.get("/").status_code == 200

    segment = stored_trace(store).segments[0]
    assert segment.type is SegmentType.HANDLER
    assert segment.name == "async_endpoint"
    assert segment.status is SegmentStatus.OK
    assert segment.attributes == (("execution_mode", "async_wall_clock"),)


def test_sync_handler_context_propagates_and_records_thread_pool_wall_clock() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    @app.get("/")
    def sync_endpoint() -> dict[str, bool]:
        return {"has_collector": current_collector() is not None}

    with (
        installed_handler_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        response = client.get("/")

    assert response.json() == {"has_collector": True}
    segment = stored_trace(store).segments[0]
    assert segment.status is SegmentStatus.OK
    assert segment.attributes == (("execution_mode", "thread_pool_wall_clock"),)


def test_handler_exception_is_recorded_without_changing_behavior() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    @app.get("/")
    async def failing_endpoint() -> None:
        raise ValueError("expected handler failure")

    with (
        installed_handler_instrumentation(),
        TestClient(
            LatensightMiddleware(app, store=store),
            raise_server_exceptions=False,
        ) as client,
    ):
        assert client.get("/").status_code == 500

    segment = stored_trace(store).segments[0]
    assert segment.status is SegmentStatus.ERROR
    assert segment.error is not None
    assert segment.error.type == "ValueError"
    assert segment.error.message == "expected handler failure"


def test_streaming_handler_ends_when_response_object_is_created() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    async def chunks() -> AsyncIterator[bytes]:
        yield b"first"
        await asyncio.sleep(0)
        yield b"second"

    @app.get("/")
    async def streaming_endpoint() -> StreamingResponse:
        return StreamingResponse(chunks())

    with (
        installed_handler_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        assert client.get("/").content == b"firstsecond"

    trace = stored_trace(store)
    segment = trace.segments[0]
    assert segment.end_ns is not None
    assert trace.response_body_completed_ns is not None
    assert segment.end_ns <= trace.response_body_completed_ns


def test_reference_counted_install_and_exact_restore() -> None:
    original = fastapi.routing.run_endpoint_function
    first_owner = object()
    second_owner = object()

    handler_instrumentation.install(first_owner)
    wrapped = fastapi.routing.run_endpoint_function
    handler_instrumentation.install(first_owner)
    handler_instrumentation.install(second_owner)
    handler_instrumentation.remove(first_owner)

    assert wrapped is not original
    assert fastapi.routing.run_endpoint_function is wrapped

    handler_instrumentation.remove(second_owner)
    assert fastapi.routing.run_endpoint_function is original


def test_wrapper_is_no_op_without_active_collector() -> None:
    app = FastAPI()

    @app.get("/")
    async def endpoint() -> dict[str, str]:
        return {"status": "ok"}

    with installed_handler_instrumentation(), TestClient(app) as client:
        assert client.get("/").json() == {"status": "ok"}


def test_restore_all_removes_every_owner() -> None:
    original = fastapi.routing.run_endpoint_function
    handler_instrumentation.install(object())
    handler_instrumentation.install(object())

    handler_instrumentation.restore_all()

    assert fastapi.routing.run_endpoint_function is original
    handler_instrumentation.restore_all()


def test_install_fails_fast_when_target_was_replaced() -> None:
    instrumentation = HandlerInstrumentation()
    original = fastapi.routing.run_endpoint_function

    async def replacement(**_kwargs: object) -> None:
        return None

    fastapi.routing.run_endpoint_function = cast(Any, replacement)
    try:
        with pytest.raises(
            RuntimeError,
            match=r"FastAPI handler instrumentation target was replaced\.",
        ):
            instrumentation.install(object())
    finally:
        fastapi.routing.run_endpoint_function = cast(Any, original)


async def test_finalized_collector_falls_back_without_recording_segment() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)
    snapshot = collector.finalize()

    async def endpoint() -> str:
        return "ok"

    with installed_handler_instrumentation():
        result = await fastapi.routing.run_endpoint_function(
            dependant=Dependant(call=endpoint),
            values={},
            is_coroutine=True,
        )

    reset_request_context(context_token)
    assert result == "ok"
    assert snapshot.segments == ()
    assert collector.late_segment_count == 1


async def test_cancelled_handler_records_cancelled_status() -> None:
    collector = make_collector()
    context_token = bind_request_context(collector)

    async def endpoint() -> None:
        raise asyncio.CancelledError

    with installed_handler_instrumentation(), pytest.raises(asyncio.CancelledError):
        await fastapi.routing.run_endpoint_function(
            dependant=Dependant(call=endpoint),
            values={},
            is_coroutine=True,
        )

    reset_request_context(context_token)
    segment = collector.finalize().segments[0]
    assert segment.status is SegmentStatus.CANCELLED
    assert segment.error is not None
    assert segment.error.type == "CancelledError"

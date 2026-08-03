import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from fastapi_lens.collector import TraceCollector
from fastapi_lens.context import bind_request_context, reset_request_context
from fastapi_lens.instrumentation.handler import handler_instrumentation
from fastapi_lens.instrumentation.sqlalchemy import (
    SQLALCHEMY_INTERNAL_EXECUTION_OPTION,
    SqlAlchemyInstrumentation,
)
from fastapi_lens.middleware import LensMiddleware
from fastapi_lens.models import (
    RequestTrace,
    RequestTraceSnapshot,
    SegmentStatus,
    SegmentType,
)
from fastapi_lens.storage.memory import MemoryTraceStore


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


def stored_trace(store: MemoryTraceStore) -> RequestTraceSnapshot:
    return asyncio.run(store.list())[0]


@contextmanager
def installed_handler() -> Iterator[None]:
    owner = object()
    handler_instrumentation.install(owner)
    try:
        yield
    finally:
        handler_instrumentation.remove(owner)


def test_sync_query_is_associated_with_the_handler_without_bind_values() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    engine = create_engine("sqlite://")
    instrumentation = SqlAlchemyInstrumentation()
    owner = object()
    instrumentation.register(engine, owner)

    @app.get("/")
    async def endpoint() -> int:
        with engine.connect() as connection:
            return cast(int, connection.scalar(text("select 42, 'private-value'")))

    try:
        with (
            installed_handler(),
            TestClient(LensMiddleware(app, store=store)) as client,
        ):
            response = client.get("/")
    finally:
        instrumentation.unregister(engine, owner)
        engine.dispose()

    assert response.json() == 42
    trace = stored_trace(store)
    handler = next(
        segment for segment in trace.segments if segment.type is SegmentType.HANDLER
    )
    query = next(
        segment for segment in trace.segments if segment.type is SegmentType.SQL
    )
    attributes = dict(query.attributes)
    assert query.name == "SELECT query"
    assert query.status is SegmentStatus.OK
    assert query.duration_ms is not None
    assert query.duration_ms >= 0
    assert query.parent_id == handler.id
    assert attributes["statement"] == "select ?, ?"
    assert attributes["operation"] == "SELECT"
    assert attributes["dialect"] == "sqlite"
    assert attributes["executemany"] is False
    assert len(str(attributes["statement_fingerprint"])) == 64
    assert "42" not in str(attributes)
    assert "private-value" not in str(attributes)


def test_executemany_records_reliable_row_count() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    engine = create_engine("sqlite://")
    instrumentation = SqlAlchemyInstrumentation()
    owner = object()
    instrumentation.register(engine, owner)

    @app.post("/")
    async def endpoint() -> dict[str, bool]:
        with engine.begin() as connection:
            connection.execute(text("create table items (value integer)"))
            connection.execute(
                text("insert into items (value) values (:value)"),
                [{"value": 1}, {"value": 2}],
            )
        return {"ok": True}

    try:
        with TestClient(LensMiddleware(app, store=store)) as client:
            assert client.post("/").json() == {"ok": True}
    finally:
        instrumentation.unregister(engine, owner)
        engine.dispose()

    queries = [
        segment
        for segment in stored_trace(store).segments
        if segment.type is SegmentType.SQL
    ]
    insert = next(query for query in queries if query.name == "INSERT query")
    assert dict(insert.attributes)["executemany"] is True
    assert dict(insert.attributes)["row_count"] == 2


def test_failed_query_records_safe_error_and_preserves_exception_type() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    engine = create_engine("sqlite://")
    instrumentation = SqlAlchemyInstrumentation()
    owner = object()
    instrumentation.register(engine, owner)

    @app.get("/")
    async def endpoint() -> None:
        with engine.connect() as connection:
            connection.execute(
                text("select * from missing_table where secret_id = 987654")
            )

    try:
        with (
            pytest.raises(OperationalError),
            TestClient(LensMiddleware(app, store=store)) as client,
        ):
            client.get("/")
    finally:
        instrumentation.unregister(engine, owner)
        engine.dispose()

    query = stored_trace(store).segments[0]
    assert query.status is SegmentStatus.ERROR
    assert query.error is not None
    assert query.error.type == "OperationalError"
    assert query.error.message == "Database execution failed."
    assert dict(query.attributes)["statement"] == (
        "select * from missing_table where secret_id = ?"
    )
    assert "987654" not in str(query.attributes)


def test_unregistered_and_internal_queries_are_not_captured() -> None:
    collector = make_collector()
    token = bind_request_context(collector)
    engine = create_engine("sqlite://")
    instrumentation = SqlAlchemyInstrumentation()
    owner = object()

    try:
        with engine.connect() as connection:
            assert connection.scalar(text("select 1")) == 1
        instrumentation.register(engine, owner)
        with engine.connect() as connection:
            internal_connection = connection.execution_options(
                **{SQLALCHEMY_INTERNAL_EXECUTION_OPTION: True}
            )
            assert internal_connection.scalar(text("select 2")) == 2
    finally:
        instrumentation.unregister(engine, owner)
        engine.dispose()
        reset_request_context(token)

    assert collector.finalize().segments == ()


async def test_async_engine_uses_its_sync_engine_event_target() -> None:
    collector = make_collector()
    token = bind_request_context(collector)
    engine = create_async_engine("sqlite+aiosqlite://")
    instrumentation = SqlAlchemyInstrumentation()
    owner = object()
    instrumentation.register(engine, owner)

    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("select 7")) == 7
    finally:
        instrumentation.unregister(engine, owner)
        await engine.dispose()
        reset_request_context(token)

    segment = collector.finalize().segments[0]
    assert segment.type is SegmentType.SQL
    assert segment.status is SegmentStatus.OK
    assert dict(segment.attributes)["dialect"] == "sqlite"


def test_owner_registration_is_idempotent_and_reference_counted() -> None:
    engine = create_engine("sqlite://")
    instrumentation = SqlAlchemyInstrumentation()
    first_owner = object()
    second_owner = object()

    instrumentation.register(engine, first_owner)
    instrumentation.register(engine, first_owner)
    instrumentation.register(engine, second_owner)

    assert instrumentation.registered_engine_count == 1
    assert instrumentation.is_registered(engine) is True

    instrumentation.unregister(engine, first_owner)
    assert instrumentation.is_registered(engine) is True

    instrumentation.unregister(engine, second_owner)
    assert instrumentation.registered_engine_count == 0
    assert instrumentation.is_registered(engine) is False
    instrumentation.unregister(engine, second_owner)
    engine.dispose()


def test_unregister_owner_and_restore_all_cover_multiple_engines() -> None:
    first_engine = create_engine("sqlite://")
    second_engine = create_engine("sqlite://")
    instrumentation = SqlAlchemyInstrumentation()
    first_owner = object()
    second_owner = object()

    instrumentation.register(first_engine, first_owner)
    instrumentation.register(first_engine, second_owner)
    instrumentation.register(second_engine, first_owner)

    instrumentation.unregister_owner(first_owner)
    assert instrumentation.is_registered(first_engine) is True
    assert instrumentation.is_registered(second_engine) is False

    instrumentation.restore_all()
    assert instrumentation.registered_engine_count == 0
    instrumentation.restore_all()
    first_engine.dispose()
    second_engine.dispose()

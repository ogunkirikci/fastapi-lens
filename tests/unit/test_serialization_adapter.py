import asyncio
import inspect
from datetime import UTC, datetime

import fastapi
import pytest

from fastapi_lens.collector import TraceCollector
from fastapi_lens.context import bind_request_context, reset_request_context
from fastapi_lens.instrumentation.serialization import SerializationInstrumentation
from fastapi_lens.models import RequestTrace, SegmentStatus


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


def test_current_capabilities_are_exposed() -> None:
    capabilities = SerializationInstrumentation().capabilities

    assert capabilities.endpoint_context is True
    assert capabilities.dump_json is True


def test_unsupported_fastapi_version_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrumentation = SerializationInstrumentation()
    monkeypatch.setattr(fastapi, "__version__", "0.142.0")

    with pytest.raises(
        RuntimeError,
        match=r"FastAPI 0\.142\.0 has no serialization adapter\.",
    ):
        instrumentation.install(object())


def test_unsupported_signature_fails_fast() -> None:
    instrumentation = SerializationInstrumentation()

    async def unsupported(*, response_content: object) -> object:
        return response_content

    instrumentation._original = unsupported

    with pytest.raises(
        RuntimeError,
        match=r"FastAPI serialize_response has an unsupported signature\.",
    ):
        instrumentation.install(object())


def test_unreadable_source_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    instrumentation = SerializationInstrumentation()

    def unreadable_source(_target: object) -> str:
        raise OSError("source unavailable")

    monkeypatch.setattr(inspect, "getsource", unreadable_source)

    with pytest.raises(
        RuntimeError,
        match=r"FastAPI serialize_response source cannot be verified\.",
    ):
        instrumentation.install(object())


def test_behavior_guard_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    instrumentation = SerializationInstrumentation()
    monkeypatch.setattr(inspect, "getsource", lambda _target: "async def hook(): pass")

    with pytest.raises(
        RuntimeError,
        match=r"FastAPI serialize_response behavior guard did not match\.",
    ):
        instrumentation.install(object())


async def test_finalized_collector_falls_back_without_a_segment() -> None:
    collector = make_collector()
    snapshot = collector.finalize()
    token = bind_request_context(collector)
    instrumentation = SerializationInstrumentation()

    try:
        result = await instrumentation._wrapper(response_content={"value": 7})
    finally:
        reset_request_context(token)

    assert result == {"value": 7}
    assert snapshot.segments == ()
    assert collector.late_segment_count == 1


async def test_cancelled_serialization_records_cancelled_status() -> None:
    collector = make_collector()
    token = bind_request_context(collector)
    instrumentation = SerializationInstrumentation()

    async def cancelled(**_kwargs: object) -> None:
        raise asyncio.CancelledError

    instrumentation._original = cancelled
    instrumentation._wrapper = instrumentation._make_wrapper()
    try:
        with pytest.raises(asyncio.CancelledError):
            await instrumentation._wrapper(response_content={"value": 7})
    finally:
        reset_request_context(token)

    segment = collector.finalize().segments[0]
    assert segment.status is SegmentStatus.CANCELLED
    assert segment.error is not None
    assert segment.error.type == "CancelledError"

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import fastapi.routing
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_lens.instrumentation.serialization import (
    SerializationInstrumentation,
    serialization_instrumentation,
)
from fastapi_lens.middleware import LensMiddleware
from fastapi_lens.models import RequestTraceSnapshot, SegmentStatus, SegmentType
from fastapi_lens.storage.memory import MemoryTraceStore

_routing: Any = fastapi.routing


@contextmanager
def installed_serialization_instrumentation() -> Iterator[None]:
    owner = object()
    serialization_instrumentation.install(owner)
    try:
        yield
    finally:
        serialization_instrumentation.remove(owner)


def stored_trace(store: MemoryTraceStore) -> RequestTraceSnapshot:
    return asyncio.run(store.list())[0]


def test_response_model_serialization_records_one_combined_segment() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    class Output(BaseModel):
        value: int

    @app.get("/", response_model=Output)
    async def endpoint() -> dict[str, int]:
        return {"value": 7}

    with (
        installed_serialization_instrumentation(),
        TestClient(LensMiddleware(app, store=store)) as client,
    ):
        response = client.get("/")

    assert response.json() == {"value": 7}
    segment = stored_trace(store).segments[0]
    assert segment.type is SegmentType.SERIALIZATION
    assert segment.name == "response serialization"
    assert segment.status is SegmentStatus.OK
    assert segment.attributes == (
        ("handler_mode", "async"),
        ("has_response_model", True),
    )


def test_sync_handler_serialization_is_labeled_without_splitting_subphases() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    class Output(BaseModel):
        value: int

    @app.get("/", response_model=Output)
    def endpoint() -> dict[str, int]:
        return {"value": 7}

    with (
        installed_serialization_instrumentation(),
        TestClient(LensMiddleware(app, store=store)) as client,
    ):
        assert client.get("/").json() == {"value": 7}

    segment = stored_trace(store).segments[0]
    assert segment.attributes == (
        ("handler_mode", "sync"),
        ("has_response_model", True),
    )


def test_json_encoding_without_a_response_model_is_profiled() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    @app.get("/", response_model=None)
    async def endpoint() -> dict[str, object]:
        return {"items": [1, 2, 3]}

    with (
        installed_serialization_instrumentation(),
        TestClient(LensMiddleware(app, store=store)) as client,
    ):
        assert client.get("/").json() == {"items": [1, 2, 3]}

    segment = stored_trace(store).segments[0]
    assert segment.attributes == (
        ("handler_mode", "async"),
        ("has_response_model", False),
    )


@pytest.mark.parametrize("path", ["/json", "/stream"])
def test_custom_and_streaming_responses_have_no_serialization_segment(
    path: str,
) -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    async def chunks() -> AsyncIterator[bytes]:
        yield b"first"
        await asyncio.sleep(0)
        yield b"second"

    @app.get("/json")
    async def json_endpoint() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/stream")
    async def stream_endpoint() -> StreamingResponse:
        return StreamingResponse(chunks())

    with (
        installed_serialization_instrumentation(),
        TestClient(LensMiddleware(app, store=store)) as client,
    ):
        response = client.get(path)

    assert response.status_code == 200
    assert stored_trace(store).segments == ()


def test_response_validation_error_is_recorded_without_behavior_change() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    class Output(BaseModel):
        value: int

    @app.get("/", response_model=Output)
    async def endpoint() -> dict[str, str]:
        return {"value": "not-an-integer"}

    with (
        installed_serialization_instrumentation(),
        TestClient(
            LensMiddleware(app, store=store),
            raise_server_exceptions=False,
        ) as client,
    ):
        assert client.get("/").status_code == 500

    segment = stored_trace(store).segments[0]
    assert segment.status is SegmentStatus.ERROR
    assert segment.error is not None
    assert segment.error.type == "ResponseValidationError"


def test_reference_counted_install_and_exact_restore() -> None:
    original = _routing.serialize_response
    first_owner = object()
    second_owner = object()

    serialization_instrumentation.install(first_owner)
    wrapper = _routing.serialize_response
    installed_before_removal = serialization_instrumentation.installed
    serialization_instrumentation.install(first_owner)
    serialization_instrumentation.install(second_owner)
    serialization_instrumentation.remove(first_owner)

    assert installed_before_removal is True
    assert wrapper is not original
    assert id(_routing.serialize_response) == id(wrapper)

    serialization_instrumentation.remove(second_owner)
    installed_after_removal = serialization_instrumentation.installed
    assert installed_after_removal is False
    assert id(_routing.serialize_response) == id(original)


def test_install_fails_fast_when_target_was_replaced() -> None:
    instrumentation = SerializationInstrumentation()
    original = _routing.serialize_response

    async def replacement(**_kwargs: object) -> None:
        return None

    _routing.serialize_response = replacement
    try:
        with pytest.raises(
            RuntimeError,
            match=r"FastAPI serialization instrumentation target was replaced\.",
        ):
            instrumentation.install(object())
    finally:
        _routing.serialize_response = original


def test_no_active_trace_is_a_no_op_and_restore_all_is_idempotent() -> None:
    app = FastAPI()

    @app.get("/")
    async def endpoint() -> dict[str, str]:
        return {"status": "ok"}

    serialization_instrumentation.install(object())
    serialization_instrumentation.install(object())
    try:
        with TestClient(app) as client:
            assert client.get("/").json() == {"status": "ok"}
    finally:
        serialization_instrumentation.restore_all()
        serialization_instrumentation.restore_all()

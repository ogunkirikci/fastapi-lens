"""Cross-version smoke test for the response serialization adapter."""

import asyncio
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_lens.instrumentation.serialization import serialization_instrumentation
from fastapi_lens.middleware import LensMiddleware
from fastapi_lens.models import SegmentStatus, SegmentType
from fastapi_lens.storage.memory import MemoryTraceStore


def run() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    class Output(BaseModel):
        value: int

    @app.get("/model", response_model=Output)
    async def model_endpoint() -> dict[str, int]:
        return {"value": 7}

    @app.get("/stream")
    async def stream_endpoint() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"body"

        return StreamingResponse(body())

    owner = object()
    serialization_instrumentation.install(owner)
    try:
        with TestClient(LensMiddleware(app, store=store)) as client:
            assert client.get("/model").json() == {"value": 7}
            assert client.get("/stream").content == b"body"
    finally:
        serialization_instrumentation.remove(owner)

    traces = {trace.path: trace for trace in asyncio.run(store.list())}
    model_trace = traces["/model"]
    streaming_trace = traces["/stream"]
    assert len(model_trace.segments) == 1
    assert model_trace.segments[0].type is SegmentType.SERIALIZATION
    assert model_trace.segments[0].status is SegmentStatus.OK
    assert streaming_trace.segments == ()


if __name__ == "__main__":
    run()

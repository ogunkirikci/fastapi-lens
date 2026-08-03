"""Cross-version smoke test for the versioned dependency solver adapter."""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fastapi_latensight.instrumentation.dependencies import dependency_instrumentation
from fastapi_latensight.middleware import LatensightMiddleware
from fastapi_latensight.models import DependencyCacheStatus, SegmentType
from fastapi_latensight.storage.memory import MemoryTraceStore


def run() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    calls = 0
    cleanup: list[str] = []

    async def dependency() -> int:
        nonlocal calls
        calls += 1
        return calls

    async def resource() -> AsyncIterator[str]:
        yield "resource"
        cleanup.append("resource")

    @app.get("/")
    async def endpoint(
        first: Annotated[int, Depends(dependency)],
        cached: Annotated[int, Depends(dependency)],
        value: Annotated[str, Depends(resource, scope="request")],
    ) -> list[object]:
        return [first, cached, value]

    owner = object()
    dependency_instrumentation.install(owner)
    try:
        with TestClient(LatensightMiddleware(app, store=store)) as client:
            response = client.get("/")
    finally:
        dependency_instrumentation.remove(owner)

    assert response.json() == [1, 1, "resource"]
    assert cleanup == ["resource"]
    trace = asyncio.run(store.list())[0]
    assert [node.cache_status for node in trace.logical_dependencies] == [
        DependencyCacheStatus.MISS,
        DependencyCacheStatus.HIT,
        DependencyCacheStatus.MISS,
    ]
    assert [segment.type for segment in trace.segments] == [
        SegmentType.DEPENDENCY_SETUP,
        SegmentType.DEPENDENCY_SETUP,
        SegmentType.DEPENDENCY_CLEANUP,
    ]


if __name__ == "__main__":
    run()

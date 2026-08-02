from contextvars import ContextVar
from typing import Annotated

import fastapi.routing
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from .probes import ReferenceCountedAsyncPatch, RouteDependencyWrapperProbe


def _assert_patch_state(
    patch: ReferenceCountedAsyncPatch,
    *,
    installed: bool,
) -> None:
    assert patch.installed is installed


def test_shared_route_wrapper_identity_preserves_dependency_cache() -> None:
    app = FastAPI()
    calls = 0

    async def dependency() -> str:
        nonlocal calls
        calls += 1
        return "value"

    @app.get("/")
    async def endpoint(
        first: Annotated[str, Depends(dependency)],
        second: Annotated[str, Depends(dependency)],
    ) -> list[str]:
        return [first, second]

    probe = RouteDependencyWrapperProbe()
    probe.install(app)
    try:
        with TestClient(app) as client:
            response = client.get("/")
    finally:
        probe.restore()

    assert response.json() == ["value", "value"]
    assert calls == 1
    assert [event.name for event in probe.events] == ["dependency"]


def test_route_wrapper_breaks_late_dependency_override_lookup() -> None:
    app = FastAPI()

    async def dependency() -> str:
        return "original"

    async def override() -> str:
        return "override"

    @app.get("/")
    async def endpoint(value: Annotated[str, Depends(dependency)]) -> str:
        return value

    probe = RouteDependencyWrapperProbe()
    probe.install(app)
    app.dependency_overrides[dependency] = override
    try:
        with TestClient(app) as client:
            wrapped_response = client.get("/")
    finally:
        probe.restore()

    with TestClient(app) as client:
        restored_response = client.get("/")

    assert wrapped_response.json() == "original"
    assert restored_response.json() == "override"


def test_native_cache_and_use_cache_false_behavior() -> None:
    app = FastAPI()
    calls = 0

    def dependency() -> int:
        nonlocal calls
        calls += 1
        return calls

    @app.get("/")
    async def endpoint(
        first: Annotated[int, Depends(dependency)],
        cached: Annotated[int, Depends(dependency)],
        uncached: Annotated[int, Depends(dependency, use_cache=False)],
    ) -> list[int]:
        return [first, cached, uncached]

    with TestClient(app) as client:
        response = client.get("/")

    assert response.json() == [1, 1, 2]
    assert calls == 2


def test_context_var_reaches_sync_dependency_thread() -> None:
    app = FastAPI()
    marker: ContextVar[str] = ContextVar("spike_marker", default="missing")

    def dependency() -> str:
        return marker.get()

    @app.middleware("http")
    async def set_marker(request: object, call_next: object) -> object:
        token = marker.set("request-context")
        try:
            return await call_next(request)  # type: ignore[operator]
        finally:
            marker.reset(token)

    @app.get("/")
    async def endpoint(value: Annotated[str, Depends(dependency)]) -> str:
        return value

    with TestClient(app) as client:
        response = client.get("/")

    assert response.json() == "request-context"


def test_handler_and_serialization_boundaries_are_patchable() -> None:
    events: list[str] = []
    handler_patch = ReferenceCountedAsyncPatch(
        fastapi.routing,
        "run_endpoint_function",
        events,
    )
    serialization_patch = ReferenceCountedAsyncPatch(
        fastapi.routing,
        "serialize_response",
        events,
    )
    owner = object()
    handler_patch.install(owner)
    serialization_patch.install(owner)
    try:
        app = FastAPI()

        @app.get("/model", response_model=dict[str, str])
        async def model_endpoint() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/response")
        async def response_endpoint() -> JSONResponse:
            return JSONResponse({"status": "ok"})

        with TestClient(app) as client:
            assert client.get("/model").status_code == 200
            model_events = events.copy()
            events.clear()
            assert client.get("/response").status_code == 200
            response_events = events.copy()
    finally:
        handler_patch.remove(owner)
        serialization_patch.remove(owner)

    assert model_events == [
        "run_endpoint_function.start",
        "run_endpoint_function.end",
        "serialize_response.start",
        "serialize_response.end",
    ]
    assert response_events == [
        "run_endpoint_function.start",
        "run_endpoint_function.end",
    ]
    assert not handler_patch.installed
    assert not serialization_patch.installed


def test_reference_counted_patch_survives_one_app_removal() -> None:
    events: list[str] = []
    patch = ReferenceCountedAsyncPatch(
        fastapi.routing,
        "serialize_response",
        events,
    )
    first_app = FastAPI()
    second_app = FastAPI()

    @first_app.get("/")
    async def first_endpoint() -> dict[str, int]:
        return {"app": 1}

    @second_app.get("/")
    async def second_endpoint() -> dict[str, int]:
        return {"app": 2}

    patch.install(first_app)
    patch.install(second_app)
    try:
        _assert_patch_state(patch, installed=True)
        patch.remove(first_app)
        _assert_patch_state(patch, installed=True)
        with TestClient(second_app) as client:
            assert client.get("/").json() == {"app": 2}
        assert events == ["serialize_response.start", "serialize_response.end"]

        events.clear()
        patch.remove(second_app)
        _assert_patch_state(patch, installed=False)
        with TestClient(first_app) as client:
            assert client.get("/").json() == {"app": 1}
        assert events == []
    finally:
        patch.restore_all()

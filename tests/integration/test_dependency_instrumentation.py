import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import fastapi.routing
import pytest
from fastapi import (
    BackgroundTasks,
    Cookie,
    Depends,
    FastAPI,
    Header,
    Request,
    Response,
    Security,
    WebSocket,
)
from fastapi.dependencies import utils as dependency_utils
from fastapi.security import SecurityScopes
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.requests import HTTPConnection
from starlette.types import Message, Receive, Scope, Send

from fastapi_latensight.collector import TraceCollector
from fastapi_latensight.context import (
    bind_request_context,
    current_collector,
    reset_request_context,
)
from fastapi_latensight.instrumentation.dependencies import (
    DependencyInstrumentation,
    dependency_instrumentation,
)
from fastapi_latensight.middleware import LatensightMiddleware
from fastapi_latensight.models import (
    DependencyCacheStatus,
    DependencyScope,
    RequestTrace,
    RequestTraceSnapshot,
    SegmentStatus,
    SegmentType,
)
from fastapi_latensight.storage.memory import MemoryTraceStore

_routing: Any = fastapi.routing


@contextmanager
def installed_dependency_instrumentation() -> Iterator[None]:
    owner = object()
    dependency_instrumentation.install(owner)
    try:
        yield
    finally:
        dependency_instrumentation.remove(owner)


def stored_trace(store: MemoryTraceStore) -> RequestTraceSnapshot:
    return asyncio.run(store.list())[0]


def test_sync_async_nested_and_callable_dependencies_are_profiled() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    async def async_child() -> str:
        await asyncio.sleep(0)
        return "child"

    def sync_parent(value: Annotated[str, Depends(async_child)]) -> str:
        assert current_collector() is not None
        return f"{value}-parent"

    class CallableDependency:
        def __call__(self) -> str:
            return "callable"

    callable_dependency = CallableDependency()

    @app.get("/")
    async def endpoint(
        nested: Annotated[str, Depends(sync_parent)],
        callable_value: Annotated[str, Depends(callable_dependency)],
    ) -> list[str]:
        return [nested, callable_value]

    with (
        installed_dependency_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        response = client.get("/")

    assert response.json() == ["child-parent", "callable"]
    trace = stored_trace(store)
    nodes = {node.name: node for node in trace.logical_dependencies}
    assert nodes["async_child"].parent_id == nodes["sync_parent"].id
    assert nodes["sync_parent"].parent_id is None
    assert nodes["CallableDependency"].parent_id is None

    segments = {segment.name: segment for segment in trace.segments}
    assert segments["async_child"].attributes == (
        ("cache_status", "miss"),
        ("execution_mode", "async"),
    )
    assert segments["sync_parent"].attributes == (
        ("cache_status", "miss"),
        ("execution_mode", "thread_pool"),
    )
    assert segments["CallableDependency"].status is SegmentStatus.OK


def test_cache_hits_and_bypasses_have_truthful_metadata() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
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

    with (
        installed_dependency_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        response = client.get("/")

    assert response.json() == [1, 1, 2]
    assert calls == 2
    trace = stored_trace(store)
    first, cached, uncached = trace.logical_dependencies
    assert first.cache_status is DependencyCacheStatus.MISS
    assert cached.cache_status is DependencyCacheStatus.HIT
    assert cached.cached_from_id == first.id
    assert cached.setup_segment_id is None
    assert cached.cleanup_segment_id is None
    assert uncached.cache_status is DependencyCacheStatus.BYPASS
    assert len(trace.segments) == 2
    assert [segment.logical_dependency_id for segment in trace.segments] == [
        first.id,
        uncached.id,
    ]


def test_bypass_can_seed_the_native_cache_for_a_later_cached_occurrence() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    calls = 0

    def dependency() -> int:
        nonlocal calls
        calls += 1
        return calls

    @app.get("/")
    async def endpoint(
        uncached: Annotated[int, Depends(dependency, use_cache=False)],
        cached: Annotated[int, Depends(dependency)],
    ) -> list[int]:
        return [uncached, cached]

    with (
        installed_dependency_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        response = client.get("/")

    assert response.json() == [1, 1]
    assert calls == 1
    uncached, cached = stored_trace(store).logical_dependencies
    assert uncached.cache_status is DependencyCacheStatus.BYPASS
    assert cached.cache_status is DependencyCacheStatus.HIT
    assert cached.cached_from_id == uncached.id


def test_dependency_override_preserves_original_lookup_identity() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    async def original() -> str:
        return "original"

    async def override() -> str:
        return "override"

    @app.get("/")
    async def endpoint(value: Annotated[str, Depends(original)]) -> str:
        return value

    app.dependency_overrides[original] = override

    with (
        installed_dependency_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        response = client.get("/")

    assert response.json() == "override"
    trace = stored_trace(store)
    assert trace.logical_dependencies[0].name == "override"
    assert trace.segments[0].name == "override"


def test_solver_preserves_request_parameters_body_and_special_injections() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    class Payload(BaseModel):
        name: str

    async def request_context(
        request: Request,
        connection: HTTPConnection,
        response: Response,
        background_tasks: BackgroundTasks,
    ) -> str:
        response.headers["x-dependency"] = "observed"
        background_tasks.add_task(lambda: None)
        assert connection.scope is request.scope
        return request.method

    async def authorize(security_scopes: SecurityScopes) -> list[str]:
        return security_scopes.scopes

    @app.post("/items/{item_id}")
    async def endpoint(
        item_id: int,
        payload: Payload,
        context: Annotated[str, Depends(request_context)],
        scopes: Annotated[list[str], Security(authorize, scopes=["items:read"])],
        query: str,
        x_token: Annotated[str, Header()],
        session: Annotated[str, Cookie()],
    ) -> dict[str, object]:
        return {
            "item_id": item_id,
            "name": payload.name,
            "context": context,
            "scopes": scopes,
            "query": query,
            "token": x_token,
            "session": session,
        }

    with (
        installed_dependency_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        client.cookies.set("session", "session-id")
        response = client.post(
            "/items/7",
            params={"query": "active"},
            headers={"x-token": "secret"},
            json={"name": "widget"},
        )

    assert response.status_code == 200
    assert response.headers["x-dependency"] == "observed"
    assert response.json() == {
        "item_id": 7,
        "name": "widget",
        "context": "POST",
        "scopes": ["items:read"],
        "query": "active",
        "token": "secret",
        "session": "session-id",
    }
    assert [
        dependency.name for dependency in stored_trace(store).logical_dependencies
    ] == ["request_context", "authorize"]


def test_unnamed_route_dependency_and_background_task_injection_are_preserved() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    calls = 0

    async def route_dependency() -> None:
        nonlocal calls
        calls += 1

    @app.get("/", dependencies=[Depends(route_dependency)])
    async def endpoint(background_tasks: BackgroundTasks) -> dict[str, bool]:
        background_tasks.add_task(lambda: None)
        return {"ok": True}

    with (
        installed_dependency_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        response = client.get("/")

    assert response.json() == {"ok": True}
    assert calls == 1
    trace = stored_trace(store)
    assert trace.logical_dependencies[0].name == "route_dependency"
    assert trace.segments[0].name == "route_dependency"


def test_websocket_dependency_injection_uses_the_native_solver_branch() -> None:
    app = FastAPI()
    collector = TraceCollector(
        RequestTrace(
            schema_version="1.0",
            id="websocket-trace",
            method="WEBSOCKET",
            path="/",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            request_received_ns=1_000_000,
        )
    )

    async def socket_dependency(websocket: WebSocket) -> str:
        return websocket.url.path

    @app.websocket("/")
    async def endpoint(
        websocket: WebSocket,
        value: Annotated[str, Depends(socket_dependency)],
    ) -> None:
        await websocket.accept()
        await websocket.send_text(value)
        await websocket.close()

    async def traced_app(
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        token = bind_request_context(collector)
        try:
            await app(scope, receive, send)
        finally:
            reset_request_context(token)

    with (
        installed_dependency_instrumentation(),
        TestClient(traced_app) as client,
        client.websocket_connect("/") as websocket,
    ):
        message: Message = websocket.receive()

    assert message["type"] == "websocket.send"
    assert message["text"] == "/"
    snapshot = collector.finalize()
    assert snapshot.logical_dependencies[0].name == "socket_dependency"
    assert snapshot.segments[0].status is SegmentStatus.OK


def test_subdependency_validation_errors_skip_the_parent_callable() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    parent_called = False

    async def child(required: int) -> int:
        return required

    async def parent(value: Annotated[int, Depends(child)]) -> int:
        nonlocal parent_called
        parent_called = True
        return value

    @app.get("/")
    async def endpoint(value: Annotated[int, Depends(parent)]) -> int:
        return value

    with (
        installed_dependency_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        response = client.get("/")

    assert response.status_code == 422
    assert parent_called is False
    trace = stored_trace(store)
    assert [node.name for node in trace.logical_dependencies] == ["parent", "child"]
    assert trace.logical_dependencies[1].parent_id == trace.logical_dependencies[0].id
    assert trace.segments == ()


def test_generator_setup_cleanup_scopes_and_lifo_order_are_preserved() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    events: list[str] = []

    async def function_dependency() -> AsyncIterator[str]:
        events.append("function.setup")
        yield "function"
        events.append("function.cleanup")

    def request_outer() -> Iterator[str]:
        events.append("outer.setup")
        yield "outer"
        events.append("outer.cleanup")

    async def request_inner() -> AsyncIterator[str]:
        events.append("inner.setup")
        yield "inner"
        events.append("inner.cleanup")

    @app.get("/")
    async def endpoint(
        function_value: Annotated[
            str,
            Depends(function_dependency, scope="function"),
        ],
        outer_value: Annotated[str, Depends(request_outer, scope="request")],
        inner_value: Annotated[str, Depends(request_inner, scope="request")],
    ) -> list[str]:
        events.append("handler")
        return [function_value, outer_value, inner_value]

    with (
        installed_dependency_instrumentation(),
        TestClient(LatensightMiddleware(app, store=store)) as client,
    ):
        response = client.get("/")

    assert response.json() == ["function", "outer", "inner"]
    assert events == [
        "function.setup",
        "outer.setup",
        "inner.setup",
        "handler",
        "function.cleanup",
        "inner.cleanup",
        "outer.cleanup",
    ]

    trace = stored_trace(store)
    nodes = {node.name: node for node in trace.logical_dependencies}
    assert nodes["function_dependency"].scope is DependencyScope.FUNCTION
    assert nodes["request_outer"].scope is DependencyScope.REQUEST
    assert nodes["request_inner"].scope is DependencyScope.REQUEST
    for node in nodes.values():
        assert node.setup_segment_id is not None
        assert node.cleanup_segment_id is not None

    segments = {segment.id: segment for segment in trace.segments}
    function_cleanup = segments[
        cast(str, nodes["function_dependency"].cleanup_segment_id)
    ]
    inner_cleanup = segments[cast(str, nodes["request_inner"].cleanup_segment_id)]
    outer_cleanup = segments[cast(str, nodes["request_outer"].cleanup_segment_id)]
    assert function_cleanup.type is SegmentType.DEPENDENCY_CLEANUP
    assert function_cleanup.end_ns is not None
    assert trace.response_started_ns is not None
    assert function_cleanup.end_ns <= trace.response_started_ns
    assert trace.response_body_completed_ns is not None
    assert inner_cleanup.start_ns >= trace.response_body_completed_ns
    assert outer_cleanup.start_ns >= inner_cleanup.start_ns
    assert dict(outer_cleanup.attributes) == {
        "cache_status": "miss",
        "execution_mode": "sync_generator_thread_pool",
        "scope": "request",
    }


def test_dependency_exception_is_recorded_without_changing_behavior() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    async def failing_dependency() -> None:
        raise ValueError("expected dependency failure")

    @app.get("/")
    async def endpoint(
        _value: Annotated[None, Depends(failing_dependency)],
    ) -> None:
        raise AssertionError("handler must not run")

    with (
        installed_dependency_instrumentation(),
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
    assert segment.error.message == "expected dependency failure"


def test_generator_cleanup_exception_is_recorded_and_propagated() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    async def failing_cleanup() -> AsyncIterator[str]:
        yield "value"
        raise RuntimeError("expected cleanup failure")

    @app.get("/")
    async def endpoint(
        _value: Annotated[str, Depends(failing_cleanup, scope="request")],
    ) -> dict[str, str]:
        return {"status": "sent"}

    with (
        installed_dependency_instrumentation(),
        TestClient(
            LatensightMiddleware(app, store=store),
            raise_server_exceptions=False,
        ) as client,
    ):
        response = client.get("/")

    assert response.status_code == 200
    cleanup = next(
        segment
        for segment in stored_trace(store).segments
        if segment.type is SegmentType.DEPENDENCY_CLEANUP
    )
    assert cleanup.status is SegmentStatus.ERROR
    assert cleanup.error is not None
    assert cleanup.error.type == "RuntimeError"
    assert cleanup.error.message == "expected cleanup failure"


def test_generator_setup_exception_has_no_synthetic_cleanup_segment() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    async def failing_setup() -> AsyncIterator[str]:
        await asyncio.sleep(0)
        if current_collector() is not None:
            raise ValueError("expected setup failure")
        yield "fallback"

    @app.get("/")
    async def endpoint(
        _value: Annotated[str, Depends(failing_setup)],
    ) -> None:
        raise AssertionError("handler must not run")

    with (
        installed_dependency_instrumentation(),
        TestClient(
            LatensightMiddleware(app, store=store),
            raise_server_exceptions=False,
        ) as client,
    ):
        assert client.get("/").status_code == 500

    trace = stored_trace(store)
    assert len(trace.segments) == 1
    setup = trace.segments[0]
    assert setup.type is SegmentType.DEPENDENCY_SETUP
    assert setup.status is SegmentStatus.ERROR
    assert setup.error is not None
    assert setup.error.type == "ValueError"
    assert trace.logical_dependencies[0].cleanup_segment_id is None


def test_reference_counted_install_restores_both_exact_targets() -> None:
    original_utils = dependency_utils.solve_dependencies
    original_routing = _routing.solve_dependencies
    first_owner = object()
    second_owner = object()

    dependency_instrumentation.install(first_owner)
    wrapper = dependency_utils.solve_dependencies
    installed_before_removal = dependency_instrumentation.installed
    assert installed_before_removal is True
    dependency_instrumentation.install(first_owner)
    dependency_instrumentation.install(second_owner)
    dependency_instrumentation.remove(first_owner)

    assert wrapper is not original_utils
    assert id(dependency_utils.solve_dependencies) == id(wrapper)
    assert id(_routing.solve_dependencies) == id(wrapper)

    dependency_instrumentation.remove(second_owner)
    installed_after_removal = dependency_instrumentation.installed
    assert installed_after_removal is False
    assert id(dependency_utils.solve_dependencies) == id(original_utils)
    assert id(_routing.solve_dependencies) == id(original_routing)


def test_install_fails_fast_when_a_solver_target_was_replaced() -> None:
    instrumentation = DependencyInstrumentation()
    original = _routing.solve_dependencies

    async def replacement(**_kwargs: object) -> None:
        return None

    _routing.solve_dependencies = replacement
    try:
        with pytest.raises(
            RuntimeError,
            match=r"FastAPI dependency instrumentation targets were replaced\.",
        ):
            instrumentation.install(object())
    finally:
        _routing.solve_dependencies = original


def test_restore_all_is_idempotent_and_no_active_trace_is_a_no_op() -> None:
    app = FastAPI()

    async def dependency() -> str:
        return "value"

    @app.get("/")
    async def endpoint(value: Annotated[str, Depends(dependency)]) -> str:
        return value

    dependency_instrumentation.install(object())
    dependency_instrumentation.install(object())
    try:
        with TestClient(app) as client:
            assert client.get("/").json() == "value"
    finally:
        dependency_instrumentation.restore_all()
        dependency_instrumentation.restore_all()

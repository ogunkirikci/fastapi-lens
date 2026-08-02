import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from fastapi_lens.context import current_collector
from fastapi_lens.diagnostics.base import DiagnosticRule
from fastapi_lens.middleware import LensMiddleware, ProfilerState
from fastapi_lens.models import (
    DependencyCacheStatus,
    Diagnostic,
    LogicalDependencyNode,
    RequestTraceSnapshot,
)
from fastapi_lens.storage.memory import MemoryTraceStore


def stored_traces(store: MemoryTraceStore) -> list[RequestTraceSnapshot]:
    return asyncio.run(store.list())


def test_normal_response_records_all_lifecycle_checkpoints() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    @app.get("/items/{item_id}")
    async def endpoint(item_id: int) -> dict[str, int]:
        assert current_collector() is not None
        return {"item_id": item_id}

    with TestClient(LensMiddleware(app, store=store)) as client:
        response = client.get("/items/42")

    assert response.json() == {"item_id": 42}
    trace = stored_traces(store)[0]
    assert trace.method == "GET"
    assert trace.path == "/items/42"
    assert trace.route == "/items/{item_id}"
    assert trace.status_code == 200
    assert trace.complete is True
    assert trace.error is None
    assert trace.response_started_ns is not None
    assert trace.response_body_completed_ns is not None
    assert trace.application_completed_ns is not None
    assert (
        trace.request_received_ns
        <= trace.response_started_ns
        <= trace.response_body_completed_ns
        <= trace.application_completed_ns
    )
    assert current_collector() is None


def test_streaming_response_records_body_completion_before_app_completion() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    async def chunks() -> AsyncIterator[bytes]:
        yield b"first"
        yield b"second"

    @app.get("/stream")
    async def endpoint() -> StreamingResponse:
        return StreamingResponse(chunks())

    with TestClient(LensMiddleware(app, store=store)) as client:
        response = client.get("/stream")

    assert response.content == b"firstsecond"
    trace = stored_traces(store)[0]
    assert trace.response_body_completed_ns is not None
    assert trace.application_completed_ns is not None
    assert trace.response_body_completed_ns <= trace.application_completed_ns
    assert trace.complete is True


def test_outer_wrapper_captures_generated_500_and_original_error() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    @app.get("/error")
    async def endpoint() -> None:
        raise RuntimeError("expected failure")

    with TestClient(
        LensMiddleware(app, store=store),
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/error")

    trace = stored_traces(store)[0]
    assert response.status_code == 500
    assert trace.status_code == 500
    assert trace.complete is True
    assert trace.error is not None
    assert trace.error.type == "RuntimeError"
    assert trace.error.message == "expected failure"


def test_app_middleware_placement_records_incomplete_unhandled_error() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    app.add_middleware(LensMiddleware, store=store)

    @app.get("/error")
    async def endpoint() -> None:
        raise RuntimeError("expected failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/error")

    trace = stored_traces(store)[0]
    assert response.status_code == 500
    assert trace.status_code is None
    assert trace.response_body_completed_ns is None
    assert trace.application_completed_ns is not None
    assert trace.complete is False
    assert trace.error is not None
    assert trace.error.type == "RuntimeError"


async def test_disconnect_without_response_produces_incomplete_trace() -> None:
    store = MemoryTraceStore()

    async def disconnected_app(
        _scope: Scope,
        receive: Receive,
        _send: Send,
    ) -> None:
        assert (await receive())["type"] == "http.disconnect"

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        raise AssertionError("A disconnected request must not send a response")

    middleware = LensMiddleware(disconnected_app, store=store)
    await middleware(http_scope("/disconnect"), receive, send)

    trace = (await store.list())[0]
    assert trace.response_started_ns is None
    assert trace.response_body_completed_ns is None
    assert trace.application_completed_ns is not None
    assert trace.complete is False


async def test_cancellation_is_re_raised_and_stored_as_incomplete() -> None:
    store = MemoryTraceStore()

    async def cancelled_app(
        _scope: Scope,
        _receive: Receive,
        _send: Send,
    ) -> None:
        raise asyncio.CancelledError

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: Message) -> None:
        raise AssertionError("A cancelled request must not send a response")

    middleware = LensMiddleware(cancelled_app, store=store)
    with pytest.raises(asyncio.CancelledError):
        await middleware(http_scope("/cancel"), receive, send)

    trace = (await store.list())[0]
    assert trace.complete is False
    assert trace.error is not None
    assert trace.error.type == "CancelledError"


def test_runtime_disable_only_affects_new_requests() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    state = ProfilerState(enabled=False)

    @app.get("/")
    async def endpoint() -> dict[str, str]:
        return {"status": "ok"}

    middleware = LensMiddleware(app, store=store, state=state)
    with TestClient(middleware) as client:
        assert client.get("/").status_code == 200
        state.enable()
        assert client.get("/").status_code == 200
        state.disable()
        assert client.get("/").status_code == 200

    assert len(stored_traces(store)) == 1


async def test_active_request_finalizes_after_runtime_disable() -> None:
    store = MemoryTraceStore()
    state = ProfilerState()
    started = asyncio.Event()
    release = asyncio.Event()

    async def app(_scope: Scope, _receive: Receive, send: Send) -> None:
        started.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: Message) -> None:
        return None

    middleware = LensMiddleware(app, store=store, state=state)
    active_request = asyncio.create_task(
        middleware(http_scope("/active"), receive, send)
    )
    await started.wait()
    state.disable()
    release.set()
    await active_request
    await middleware(http_scope("/disabled"), receive, send)

    traces = await store.list()
    assert len(traces) == 1
    assert traces[0].path == "/active"
    assert traces[0].complete is True


def test_include_exclude_and_final_route_template_filters() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    @app.get("/api/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @app.get("/api/private/{item_id}")
    async def private(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    middleware = LensMiddleware(
        app,
        store=store,
        include_routes=("/api/*",),
        exclude_routes=("/api/private/{item_id}",),
    )
    with TestClient(middleware) as client:
        assert client.get("/api/items/1").status_code == 200
        assert client.get("/api/private/1").status_code == 200
        assert client.get("/health").status_code == 200

    assert [trace.route for trace in stored_traces(store)] == ["/api/items/{item_id}"]


def test_404_405_and_trailing_slash_have_explicit_filter_behavior() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    @app.get("/items")
    async def items() -> list[str]:
        return []

    with TestClient(
        LensMiddleware(app, store=store),
        follow_redirects=False,
    ) as client:
        assert client.get("/missing").status_code == 404
        assert client.post("/items").status_code == 405
        assert client.get("/items/").status_code == 307

    traces = stored_traces(store)
    assert {trace.status_code for trace in traces} == {404, 405, 307}
    missing = next(trace for trace in traces if trace.status_code == 404)
    assert missing.route is None
    method_not_allowed = next(trace for trace in traces if trace.status_code == 405)
    assert method_not_allowed.route == "/items"


def test_mounted_route_template_includes_mount_prefix() -> None:
    app = FastAPI()
    subapp = FastAPI()
    store = MemoryTraceStore()

    @subapp.get("/items/{item_id}")
    async def endpoint(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    app.mount("/api", subapp)
    with TestClient(LensMiddleware(app, store=store)) as client:
        assert client.get("/api/items/42").status_code == 200

    assert stored_traces(store)[0].route == "/api/items/{item_id}"


def test_deployment_root_path_is_not_added_to_route_template() -> None:
    app = FastAPI(root_path="/service")
    store = MemoryTraceStore()

    @app.get("/items")
    async def endpoint() -> list[str]:
        return []

    with TestClient(
        LensMiddleware(app, store=store),
        root_path="/service",
    ) as client:
        assert client.get("/items").status_code == 200

    assert stored_traces(store)[0].route == "/items"


def test_route_template_tolerates_non_string_root_path() -> None:
    scope: Any = http_scope("/items")
    scope["route"] = SimpleNamespace(path="/items")
    scope["root_path"] = 42

    assert LensMiddleware._route_template(scope, base_root_path="") == "/items"


class FindingRule:
    code = "TEST_FINDING"

    def evaluate(self, trace: RequestTraceSnapshot) -> list[Diagnostic]:
        return [
            Diagnostic(
                code=self.code,
                severity="info",
                message=f"Observed {trace.method} request.",
            )
        ]


class FailingRule:
    code = "FAILING_RULE"

    def evaluate(self, _trace: RequestTraceSnapshot) -> list[Diagnostic]:
        raise RuntimeError("diagnostic failure")


def accepts_rule(rule: DiagnosticRule) -> DiagnosticRule:
    return rule


def test_diagnostic_rules_run_and_failures_are_isolated() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    @app.get("/")
    async def endpoint() -> None:
        return None

    middleware = LensMiddleware(
        app,
        store=store,
        diagnostic_rules=(accepts_rule(FailingRule()), accepts_rule(FindingRule())),
    )
    with TestClient(middleware) as client:
        assert client.get("/").status_code == 200

    assert stored_traces(store)[0].diagnostics == (
        Diagnostic(
            code="TEST_FINDING",
            severity="info",
            message="Observed GET request.",
        ),
    )


class FailingStore:
    async def save(self, _trace: RequestTraceSnapshot) -> None:
        raise RuntimeError("storage failure")

    async def get(self, _trace_id: str) -> RequestTraceSnapshot | None:
        return None

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RequestTraceSnapshot]:
        return []

    async def clear(self) -> None:
        return None


def test_storage_failure_does_not_change_application_response() -> None:
    app = FastAPI()

    @app.get("/")
    async def endpoint() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(LensMiddleware(app, store=FailingStore())) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_collector_integrity_failure_does_not_change_application_response() -> None:
    app = FastAPI()
    store = MemoryTraceStore()

    @app.get("/")
    async def endpoint() -> dict[str, str]:
        collector = current_collector()
        assert collector is not None
        dependency = LogicalDependencyNode(
            id="dependency-1",
            trace_id=collector.trace.id,
            name="dependency",
            cache_status=DependencyCacheStatus.MISS,
        )
        dependency.cached_from_id = "invalid-cache-source"
        collector.trace.logical_dependencies.append(dependency)
        return {"status": "ok"}

    with TestClient(LensMiddleware(app, store=store)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert stored_traces(store) == []


async def test_concurrent_requests_never_mix_trace_context() -> None:
    app = FastAPI()
    store = MemoryTraceStore(max_traces=50)

    @app.get("/items/{item_id}")
    async def endpoint(item_id: int) -> dict[str, str | int]:
        collector = current_collector()
        assert collector is not None
        await asyncio.sleep(0)
        assert current_collector() is collector
        return {"item_id": item_id, "trace_id": collector.trace.id}

    middleware = LensMiddleware(app, store=store)

    async def request(index: int) -> dict[str, str | int]:
        messages: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            messages.append(message)

        await middleware(http_scope(f"/items/{index}"), receive, send)
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        parsed: dict[str, str | int] = json.loads(body)
        return parsed

    responses = await asyncio.gather(*(request(index) for index in range(20)))
    response_trace_ids = {str(response["trace_id"]) for response in responses}
    traces = await store.list(limit=20)
    assert len(response_trace_ids) == 20
    assert {trace.id for trace in traces} == response_trace_ids


async def test_non_http_scope_bypasses_tracing() -> None:
    called = False
    store = MemoryTraceStore()

    async def app(_scope: Scope, _receive: Receive, _send: Send) -> None:
        nonlocal called
        called = True

    async def receive() -> Message:
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(_message: Message) -> None:
        return None

    scope: Scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": "/ws",
        "raw_path": b"/ws",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }

    await LensMiddleware(app, store=store)(scope, receive, send)

    assert called is True
    assert await store.list() == []


async def test_non_string_initial_root_path_is_tolerated() -> None:
    store = MemoryTraceStore()

    async def app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: Message) -> None:
        return None

    scope: Any = http_scope("/items")
    scope["root_path"] = 42
    await LensMiddleware(app, store=store)(scope, receive, send)

    trace = (await store.list())[0]
    assert trace.path == "/items"
    assert trace.route is None


def http_scope(path: str) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

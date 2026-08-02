from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from .probes import LifecycleObservation, LifecycleProbeMiddleware


def test_normal_response_records_all_lifecycle_checkpoints() -> None:
    app = FastAPI()
    observations: list[LifecycleObservation] = []

    @app.get("/")
    async def endpoint() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(LifecycleProbeMiddleware(app, observations)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert len(observations) == 1
    observation = observations[0]
    assert observation.complete
    assert observation.status_code == 200
    assert observation.response_started_ns is not None
    assert observation.response_body_completed_ns is not None
    assert observation.application_completed_ns is not None
    assert (
        observation.request_received_ns
        <= observation.response_started_ns
        <= observation.response_body_completed_ns
        <= observation.application_completed_ns
    )


def test_streaming_response_separates_body_completion_from_app_completion() -> None:
    app = FastAPI()
    observations: list[LifecycleObservation] = []

    async def chunks() -> AsyncIterator[bytes]:
        yield b"first"
        yield b"second"

    @app.get("/")
    async def endpoint() -> StreamingResponse:
        return StreamingResponse(chunks())

    with TestClient(LifecycleProbeMiddleware(app, observations)) as client:
        response = client.get("/")

    assert response.content == b"firstsecond"
    observation = observations[0]
    assert observation.complete
    assert observation.body_events[-1] is False
    assert len(observation.body_events) >= 2
    assert observation.response_body_completed_ns is not None
    assert observation.application_completed_ns is not None
    assert (
        observation.response_body_completed_ns <= observation.application_completed_ns
    )


def test_dependency_scope_cleanup_order_is_observable() -> None:
    app = FastAPI()
    observations: list[LifecycleObservation] = []
    event_log: list[str] = []

    async def function_dependency() -> AsyncIterator[str]:
        event_log.append("function.setup")
        try:
            yield "function"
        finally:
            event_log.append("function.cleanup")

    async def request_dependency() -> AsyncIterator[str]:
        event_log.append("request.setup")
        try:
            yield "request"
        finally:
            event_log.append("request.cleanup")

    @app.get("/")
    async def endpoint(
        function_value: str = Depends(function_dependency, scope="function"),
        request_value: str = Depends(request_dependency, scope="request"),
    ) -> dict[str, str]:
        event_log.append("handler")
        return {"function": function_value, "request": request_value}

    wrapped_app = LifecycleProbeMiddleware(app, observations, event_log)
    with TestClient(wrapped_app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert event_log == [
        "function.setup",
        "request.setup",
        "handler",
        "function.cleanup",
        "response.start",
        "response.body.complete",
        "request.cleanup",
        "application.complete",
    ]


def test_unhandled_exception_is_complete_when_probe_wraps_fastapi() -> None:
    app = FastAPI()
    observations: list[LifecycleObservation] = []

    @app.get("/")
    async def endpoint() -> None:
        raise RuntimeError("expected spike failure")

    with TestClient(
        LifecycleProbeMiddleware(app, observations),
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/")

    assert response.status_code == 500
    observation = observations[0]
    assert observation.complete
    assert observation.status_code == 500
    assert observation.error_type == "RuntimeError"


def test_app_middleware_placement_cannot_observe_outer_500_response() -> None:
    app = FastAPI()
    observations: list[LifecycleObservation] = []
    app.add_middleware(LifecycleProbeMiddleware, observations=observations)

    @app.get("/")
    async def endpoint() -> None:
        raise RuntimeError("expected spike failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/")

    assert response.status_code == 500
    observation = observations[0]
    assert not observation.complete
    assert observation.response_started_ns is None
    assert observation.response_body_completed_ns is None
    assert observation.application_completed_ns is not None
    assert observation.error_type == "RuntimeError"


async def test_disconnect_without_response_produces_incomplete_trace() -> None:
    observations: list[LifecycleObservation] = []

    async def disconnected_app(
        _scope: Scope,
        receive: Receive,
        _send: Send,
    ) -> None:
        message = await receive()
        assert message["type"] == "http.disconnect"

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        raise AssertionError("A disconnected request must not send a response")

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    probe = LifecycleProbeMiddleware(disconnected_app, observations)
    await probe(scope, receive, send)

    observation = observations[0]
    assert not observation.complete
    assert observation.response_started_ns is None
    assert observation.response_body_completed_ns is None
    assert observation.application_completed_ns is not None

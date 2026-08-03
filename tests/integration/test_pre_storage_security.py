import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_latensight.exporters.json import trace_snapshot_to_dict
from fastapi_latensight.middleware import LatensightMiddleware
from fastapi_latensight.models import RequestTraceSnapshot
from fastapi_latensight.redaction import TraceSanitizer, TraceSanitizerConfig
from fastapi_latensight.segments import current_trace


class InspectingStore:
    def __init__(self) -> None:
        self.saved: list[RequestTraceSnapshot] = []

    async def save(self, trace: RequestTraceSnapshot) -> None:
        serialized = json.dumps(trace_snapshot_to_dict(trace))
        assert "private-authorization" not in serialized
        assert "private-password" not in serialized
        self.saved.append(trace)

    async def get(self, trace_id: str) -> RequestTraceSnapshot | None:
        return next(
            (trace for trace in self.saved if trace.id == trace_id),
            None,
        )

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RequestTraceSnapshot]:
        return self.saved[offset : offset + limit]

    async def clear(self) -> None:
        self.saved.clear()


def test_redaction_and_limits_run_before_store_save() -> None:
    app = FastAPI()
    store = InspectingStore()
    sanitizer = TraceSanitizer(
        TraceSanitizerConfig(
            max_attribute_length=20,
            max_error_length=20,
            max_trace_bytes=10_000,
        )
    )

    @app.get("/private-path-value")
    async def endpoint() -> dict[str, bool]:
        async with current_trace.segment(
            "custom-segment-name-that-is-too-long",
            attributes={
                "Authorization": "private-authorization",
                "nested": {
                    "password": "private-password",
                    "safe": "visible",
                },
            },
        ):
            return {"ok": True}

    with TestClient(
        LatensightMiddleware(
            app,
            store=store,
            sanitizer=sanitizer,
        )
    ) as client:
        assert client.get("/private-path-value").json() == {"ok": True}

    trace = store.saved[0]
    segment = trace.segments[0]
    attributes = dict(segment.attributes)
    nested = dict(attributes["nested"])  # type: ignore[arg-type]
    assert trace.path == "/private-path-value"
    assert segment.name == "custom-segment-name…"
    assert attributes["Authorization"] == "[REDACTED]"
    assert nested["password"] == "[REDACTED]"
    assert nested["safe"] == "visible"
    assert trace.diagnostics[0].code == "trace_truncated"

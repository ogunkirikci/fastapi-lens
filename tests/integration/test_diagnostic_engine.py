import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_lens.diagnostics import DiagnosticConfig, DiagnosticEngine
from fastapi_lens.middleware import LensMiddleware
from fastapi_lens.storage.memory import MemoryTraceStore


def test_builtin_diagnostic_engine_runs_before_snapshot_storage() -> None:
    app = FastAPI()
    store = MemoryTraceStore()
    engine = DiagnosticEngine(config=DiagnosticConfig(slow_request_threshold_ms=0))

    @app.get("/")
    async def endpoint() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(
        LensMiddleware(
            app,
            store=store,
            diagnostic_rules=(engine,),
        )
    ) as client:
        assert client.get("/").json() == {"ok": True}

    trace = asyncio.run(store.list())[0]
    assert [finding.code for finding in trace.diagnostics] == ["slow_request"]
    assert "response-complete duration" in trace.diagnostics[0].message

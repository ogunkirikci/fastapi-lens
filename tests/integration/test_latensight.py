import asyncio
from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from fastapi_latensight import Latensight, LatensightConfig
from fastapi_latensight.instrumentation import (
    dependency_instrumentation,
    handler_instrumentation,
    serialization_instrumentation,
)
from fastapi_latensight.instrumentation.sqlalchemy import sqlalchemy_instrumentation
from fastapi_latensight.models import SegmentType


@pytest.fixture(autouse=True)
def restore_global_instrumentation() -> Iterator[None]:
    yield
    sqlalchemy_instrumentation.restore_all()
    serialization_instrumentation.restore_all()
    dependency_instrumentation.restore_all()
    handler_instrumentation.restore_all()


async def request_value() -> int:
    return 42


def profiled_app(
    *, config: LatensightConfig | None = None
) -> tuple[FastAPI, Latensight]:
    app = FastAPI()

    @app.get("/items", response_model=dict[str, int])
    async def items(value: Annotated[int, Depends(request_value)]) -> dict[str, int]:
        return {"value": value}

    profiler = Latensight(app, config=config)
    return app, profiler


def test_latensight_attaches_complete_default_instrumentation() -> None:
    app, profiler = profiled_app()

    try:
        with TestClient(app) as client:
            response = client.get("/items")

        traces = asyncio.run(profiler.list_traces())
    finally:
        profiler.close()

    assert response.json() == {"value": 42}
    assert len(traces) == 1
    trace = traces[0]
    assert trace.route == "/items"
    assert trace.complete is True
    assert {segment.type for segment in trace.segments} >= {
        SegmentType.DEPENDENCY_SETUP,
        SegmentType.HANDLER,
        SegmentType.SERIALIZATION,
    }
    assert len(trace.logical_dependencies) == 1


def test_runtime_disable_and_enable_only_affect_new_requests() -> None:
    app, profiler = profiled_app()

    try:
        with TestClient(app) as client:
            profiler.disable()
            client.get("/items")
            profiler.enable()
            client.get("/items")

        traces = asyncio.run(profiler.list_traces())
    finally:
        profiler.close()

    assert len(traces) == 1
    assert profiler.enabled is False


def test_store_access_and_clear_are_exposed_as_async_operations() -> None:
    app, profiler = profiled_app()

    try:
        with TestClient(app) as client:
            client.get("/items")

        traces = asyncio.run(profiler.list_traces(limit=1))
        selected = asyncio.run(profiler.get_trace(traces[0].id))
        asyncio.run(profiler.clear_traces())
        remaining = asyncio.run(profiler.list_traces())
    finally:
        profiler.close()

    assert selected == traces[0]
    assert remaining == []


def test_dashboard_is_mounted_and_excluded_from_trace_capture() -> None:
    app, profiler = profiled_app(
        config=LatensightConfig(
            dashboard_enabled=True,
            environment="development",
            dashboard_path="/profiler",
        )
    )

    try:
        with TestClient(app) as client:
            client.get("/items")
            dashboard = client.get("/profiler/")
            client.get("/profiler/api/routes")

        traces = asyncio.run(profiler.list_traces())
    finally:
        profiler.close()

    assert dashboard.status_code == 200
    assert "fastapi-latensight" in dashboard.text
    assert len(traces) == 1
    assert traces[0].path == "/items"


def require_admin(
    x_admin: Annotated[str | None, Header()] = None,
) -> None:
    if x_admin != "allowed":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authorized.",
        )


def test_dashboard_authorization_is_forwarded_by_latensight() -> None:
    app = FastAPI()
    profiler = Latensight(
        app,
        config=LatensightConfig(
            dashboard_enabled=True,
            environment="production",
            allow_in_production=True,
        ),
        dashboard_dependencies=(require_admin,),
    )

    try:
        with TestClient(app) as client:
            unauthorized = client.get("/__latensight__/")
            authorized = client.get(
                "/__latensight__/",
                headers={"x-admin": "allowed"},
            )
    finally:
        profiler.close()

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_explicit_sql_registration_uses_the_configured_statement_limit() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    app = FastAPI()

    @app.get("/query")
    def query() -> dict[str, int]:
        with engine.connect() as connection:
            value = connection.execute(
                text("SELECT 1 AS deliberately_long_column_name")
            ).scalar_one()
        return {"value": value}

    profiler = Latensight(
        app,
        config=LatensightConfig(
            capture_sql=True,
            max_sql_length=12,
        ),
    )
    profiler.instrument_sqlalchemy(engine)

    try:
        with TestClient(app) as client:
            client.get("/query")
        trace = asyncio.run(profiler.list_traces())[0]
    finally:
        profiler.close()
        engine.dispose()

    query_segment = next(
        segment for segment in trace.segments if segment.type is SegmentType.SQL
    )
    statement = dict(query_segment.attributes)["statement"]
    assert isinstance(statement, str)
    assert len(statement) == 12
    assert statement.endswith("…")


def test_sql_registration_fails_when_capture_is_disabled() -> None:
    _app, profiler = profiled_app(config=LatensightConfig(capture_sql=False))
    engine = create_engine("sqlite://")

    try:
        with pytest.raises(RuntimeError, match=r"SQL capture is disabled"):
            profiler.instrument_sqlalchemy(engine)
    finally:
        profiler.close()
        engine.dispose()


def test_multiple_latensight_instances_share_and_release_global_adapters() -> None:
    _first_app, first_profiler = profiled_app()
    second_app, second_profiler = profiled_app()

    try:
        assert handler_instrumentation.installed is True
        first_profiler.close()
        assert handler_instrumentation.installed is True

        with TestClient(second_app) as client:
            client.get("/items")
        assert len(asyncio.run(second_profiler.list_traces())) == 1
    finally:
        first_profiler.close()
        second_profiler.close()

    assert handler_instrumentation.installed is False


def test_latensight_rejects_duplicate_or_late_attachment() -> None:
    app, profiler = profiled_app()
    try:
        with pytest.raises(RuntimeError, match=r"already attached"):
            Latensight(app)
    finally:
        profiler.close()

    started_app = FastAPI()
    with TestClient(started_app):
        pass
    with pytest.raises(RuntimeError, match=r"before application startup"):
        Latensight(started_app)


def test_closed_latensight_cannot_be_reenabled() -> None:
    _, profiler = profiled_app()

    profiler.close()
    profiler.close()

    with pytest.raises(RuntimeError, match=r"Latensight is closed"):
        profiler.enable()


def test_environment_enablement_applies_only_without_explicit_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FASTAPI_LATENSIGHT_ENABLED", "false")
    environment_app = FastAPI()
    environment_profiler = Latensight(environment_app)
    explicit_app = FastAPI()
    explicit_profiler = Latensight(
        explicit_app,
        config=LatensightConfig(enabled=True),
    )

    try:
        assert environment_profiler.enabled is False
        assert explicit_profiler.enabled is True
    finally:
        environment_profiler.close()
        explicit_profiler.close()

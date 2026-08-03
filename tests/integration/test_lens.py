import asyncio
from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from fastapi_lens import Lens, LensConfig
from fastapi_lens.instrumentation import (
    dependency_instrumentation,
    handler_instrumentation,
    serialization_instrumentation,
)
from fastapi_lens.instrumentation.sqlalchemy import sqlalchemy_instrumentation
from fastapi_lens.models import SegmentType


@pytest.fixture(autouse=True)
def restore_global_instrumentation() -> Iterator[None]:
    yield
    sqlalchemy_instrumentation.restore_all()
    serialization_instrumentation.restore_all()
    dependency_instrumentation.restore_all()
    handler_instrumentation.restore_all()


async def request_value() -> int:
    return 42


def profiled_app(*, config: LensConfig | None = None) -> tuple[FastAPI, Lens]:
    app = FastAPI()

    @app.get("/items", response_model=dict[str, int])
    async def items(value: Annotated[int, Depends(request_value)]) -> dict[str, int]:
        return {"value": value}

    lens = Lens(app, config=config)
    return app, lens


def test_lens_attaches_complete_default_instrumentation() -> None:
    app, lens = profiled_app()

    try:
        with TestClient(app) as client:
            response = client.get("/items")

        traces = asyncio.run(lens.list_traces())
    finally:
        lens.close()

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
    app, lens = profiled_app()

    try:
        with TestClient(app) as client:
            lens.disable()
            client.get("/items")
            lens.enable()
            client.get("/items")

        traces = asyncio.run(lens.list_traces())
    finally:
        lens.close()

    assert len(traces) == 1
    assert lens.enabled is False


def test_store_access_and_clear_are_exposed_as_async_operations() -> None:
    app, lens = profiled_app()

    try:
        with TestClient(app) as client:
            client.get("/items")

        traces = asyncio.run(lens.list_traces(limit=1))
        selected = asyncio.run(lens.get_trace(traces[0].id))
        asyncio.run(lens.clear_traces())
        remaining = asyncio.run(lens.list_traces())
    finally:
        lens.close()

    assert selected == traces[0]
    assert remaining == []


def test_dashboard_is_mounted_and_excluded_from_trace_capture() -> None:
    app, lens = profiled_app(
        config=LensConfig(
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

        traces = asyncio.run(lens.list_traces())
    finally:
        lens.close()

    assert dashboard.status_code == 200
    assert "fastapi-lens" in dashboard.text
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


def test_dashboard_authorization_is_forwarded_by_lens() -> None:
    app = FastAPI()
    lens = Lens(
        app,
        config=LensConfig(
            dashboard_enabled=True,
            environment="production",
            allow_in_production=True,
        ),
        dashboard_dependencies=(require_admin,),
    )

    try:
        with TestClient(app) as client:
            unauthorized = client.get("/__lens__/")
            authorized = client.get(
                "/__lens__/",
                headers={"x-admin": "allowed"},
            )
    finally:
        lens.close()

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

    lens = Lens(
        app,
        config=LensConfig(
            capture_sql=True,
            max_sql_length=12,
        ),
    )
    lens.instrument_sqlalchemy(engine)

    try:
        with TestClient(app) as client:
            client.get("/query")
        trace = asyncio.run(lens.list_traces())[0]
    finally:
        lens.close()
        engine.dispose()

    query_segment = next(
        segment for segment in trace.segments if segment.type is SegmentType.SQL
    )
    statement = dict(query_segment.attributes)["statement"]
    assert isinstance(statement, str)
    assert len(statement) == 12
    assert statement.endswith("…")


def test_sql_registration_fails_when_capture_is_disabled() -> None:
    _app, lens = profiled_app(config=LensConfig(capture_sql=False))
    engine = create_engine("sqlite://")

    try:
        with pytest.raises(RuntimeError, match=r"SQL capture is disabled"):
            lens.instrument_sqlalchemy(engine)
    finally:
        lens.close()
        engine.dispose()


def test_multiple_lens_instances_share_and_release_global_adapters() -> None:
    _first_app, first_lens = profiled_app()
    second_app, second_lens = profiled_app()

    try:
        assert handler_instrumentation.installed is True
        first_lens.close()
        assert handler_instrumentation.installed is True

        with TestClient(second_app) as client:
            client.get("/items")
        assert len(asyncio.run(second_lens.list_traces())) == 1
    finally:
        first_lens.close()
        second_lens.close()

    assert handler_instrumentation.installed is False


def test_lens_rejects_duplicate_or_late_attachment() -> None:
    app, lens = profiled_app()
    try:
        with pytest.raises(RuntimeError, match=r"already attached"):
            Lens(app)
    finally:
        lens.close()

    started_app = FastAPI()
    with TestClient(started_app):
        pass
    with pytest.raises(RuntimeError, match=r"before application startup"):
        Lens(started_app)


def test_closed_lens_cannot_be_reenabled() -> None:
    _, lens = profiled_app()

    lens.close()
    lens.close()

    with pytest.raises(RuntimeError, match=r"Lens is closed"):
        lens.enable()


def test_environment_enablement_applies_only_without_explicit_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FASTAPI_LENS_ENABLED", "false")
    environment_app = FastAPI()
    environment_lens = Lens(environment_app)
    explicit_app = FastAPI()
    explicit_lens = Lens(explicit_app, config=LensConfig(enabled=True))

    try:
        assert environment_lens.enabled is False
        assert explicit_lens.enabled is True
    finally:
        environment_lens.close()
        explicit_lens.close()

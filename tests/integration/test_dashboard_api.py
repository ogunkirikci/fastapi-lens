import asyncio
from datetime import UTC, datetime
from typing import Annotated

import pytest
from fastapi import Header, HTTPException, status
from fastapi.testclient import TestClient

from fastapi_lens.config import LensConfig
from fastapi_lens.dashboard import create_dashboard_app
from fastapi_lens.models import (
    DependencyCacheStatus,
    Diagnostic,
    LogicalDependencyNode,
    RequestTrace,
    SegmentStatus,
    SegmentType,
    TraceError,
    TraceSegment,
)
from fastapi_lens.security import DashboardSecurityError
from fastapi_lens.storage.memory import MemoryTraceStore

NS_PER_MS = 1_000_000


def make_trace(
    trace_id: str,
    *,
    route: str,
    duration_ms: int | None,
    order: int,
    status_code: int | None = 200,
    error: bool = False,
    sql_count: int = 0,
    dependency_count: int = 0,
) -> RequestTrace:
    base_ns = order * 1_000 * NS_PER_MS
    complete = duration_ms is not None
    segments = [
        TraceSegment(
            id=f"{trace_id}-sql-{index}",
            trace_id=trace_id,
            type=SegmentType.SQL,
            name="SELECT query",
            start_ns=base_ns + index,
            end_ns=base_ns + index + 1,
            status=SegmentStatus.OK,
        )
        for index in range(sql_count)
    ]
    dependencies = [
        LogicalDependencyNode(
            id=f"{trace_id}-dependency-{index}",
            trace_id=trace_id,
            name="dependency",
            cache_status=DependencyCacheStatus.MISS,
        )
        for index in range(dependency_count)
    ]
    return RequestTrace(
        schema_version="1.0",
        id=trace_id,
        method="GET",
        path=route.replace("{item_id}", "7"),
        route=route,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_received_ns=base_ns,
        response_started_ns=base_ns + NS_PER_MS if complete else None,
        response_body_completed_ns=(
            base_ns + duration_ms * NS_PER_MS if duration_ms is not None else None
        ),
        application_completed_ns=base_ns
        + (duration_ms if duration_ms is not None else 1) * NS_PER_MS
        + 1,
        status_code=status_code,
        segments=segments,
        logical_dependencies=dependencies,
        diagnostics=[
            Diagnostic(
                code="finding",
                severity="info",
                message="Finding.",
            )
        ],
        error=(
            TraceError(type="RuntimeError", message="Request failed.")
            if error
            else None
        ),
        complete=complete,
    )


def seeded_store() -> MemoryTraceStore:
    store = MemoryTraceStore(max_page_size=2)
    traces = [
        make_trace(
            "trace-1",
            route="/items/{item_id}",
            duration_ms=10,
            order=1,
            sql_count=2,
            dependency_count=1,
        ),
        make_trace(
            "trace-2",
            route="/items/{item_id}",
            duration_ms=20,
            order=2,
            status_code=500,
            error=True,
        ),
        make_trace(
            "trace-3",
            route="/items/{item_id}",
            duration_ms=None,
            order=3,
            status_code=None,
        ),
        make_trace(
            "trace-4",
            route="/health",
            duration_ms=30,
            order=4,
        ),
    ]
    for trace in traces:
        asyncio.run(store.save(trace.snapshot()))
    return store


def dashboard_config(**overrides: object) -> LensConfig:
    values: dict[str, object] = {
        "dashboard_enabled": True,
        "environment": "development",
    }
    values.update(overrides)
    return LensConfig(**values)  # type: ignore[arg-type]


def assert_security_headers(response: object) -> None:
    headers = response.headers  # type: ignore[attr-defined]
    assert headers["cache-control"] == "no-store"
    assert "default-src 'none'" in headers["content-security-policy"]


def test_trace_list_is_versioned_paginated_and_process_local() -> None:
    app = create_dashboard_app(
        seeded_store(),
        config=dashboard_config(),
        max_page_size=2,
    )

    with TestClient(app) as client:
        response = client.get("/api/traces", params={"limit": 2, "offset": 1})

    assert response.status_code == 200
    assert_security_headers(response)
    payload = response.json()
    assert payload["schema_version"] == "1.0"
    assert payload["process_local"] is True
    assert payload["limit"] == 2
    assert payload["offset"] == 1
    assert [item["trace_id"] for item in payload["items"]] == [
        "trace-3",
        "trace-2",
    ]
    assert payload["items"][1]["status_code"] == 500


def test_trace_list_counts_sql_dependencies_and_diagnostics() -> None:
    app = create_dashboard_app(
        seeded_store(),
        config=dashboard_config(),
        max_page_size=2,
    )

    with TestClient(app) as client:
        payload = client.get(
            "/api/traces",
            params={"limit": 2, "offset": 3},
        ).json()

    item = payload["items"][0]
    assert item["trace_id"] == "trace-1"
    assert item["sql_query_count"] == 2
    assert item["dependency_count"] == 1
    assert item["diagnostic_count"] == 1


def test_trace_detail_returns_the_versioned_public_trace_schema() -> None:
    app = create_dashboard_app(
        seeded_store(),
        config=dashboard_config(),
        max_page_size=2,
    )

    with TestClient(app) as client:
        response = client.get("/api/traces/trace-1")
        missing = client.get("/api/traces/missing")

    payload = response.json()
    assert payload["schema_version"] == "1.0"
    assert payload["process_local"] is True
    assert payload["trace"]["schema_version"] == "1.0"
    assert payload["trace"]["trace_id"] == "trace-1"
    assert payload["trace"]["segments"][0]["type"] == "sql"
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Trace not found."}
    assert_security_headers(missing)


def test_route_summary_uses_complete_trace_percentiles_and_stable_routes() -> None:
    app = create_dashboard_app(
        seeded_store(),
        config=dashboard_config(),
        max_page_size=2,
    )

    with TestClient(app) as client:
        response = client.get("/api/routes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "1.0"
    assert [item["route"] for item in payload["items"]] == [
        "/health",
        "/items/{item_id}",
    ]
    items = {item["route"]: item for item in payload["items"]}
    summary = items["/items/{item_id}"]
    assert summary == {
        "route": "/items/{item_id}",
        "request_count": 3,
        "complete_count": 2,
        "average_ms": 15.0,
        "minimum_ms": 10.0,
        "maximum_ms": 20.0,
        "p50_ms": 10.0,
        "p95_ms": 20.0,
        "p99_ms": 20.0,
        "error_count": 1,
    }


def test_clear_operation_reports_count_and_uses_the_same_store() -> None:
    store = seeded_store()
    app = create_dashboard_app(
        store,
        config=dashboard_config(),
        max_page_size=2,
    )

    with TestClient(app) as client:
        response = client.delete("/api/traces")
        remaining = client.get("/api/traces", params={"limit": 2}).json()

    assert response.json() == {
        "schema_version": "1.0",
        "process_local": True,
        "cleared_count": 4,
    }
    assert remaining["items"] == []


def test_pagination_validation_is_bounded_and_has_security_headers() -> None:
    app = create_dashboard_app(
        seeded_store(),
        config=dashboard_config(),
        max_page_size=2,
    )

    with TestClient(app) as client:
        too_large = client.get("/api/traces", params={"limit": 3})
        invalid_offset = client.get("/api/traces", params={"offset": -1})

    assert too_large.status_code == 422
    assert invalid_offset.status_code == 422
    assert_security_headers(too_large)
    assert_security_headers(invalid_offset)


def require_admin(
    x_admin: Annotated[str | None, Header()] = None,
) -> None:
    if x_admin != "allowed":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authorized.",
        )


def test_authorization_applies_to_reads_and_mutations_without_data_leakage() -> None:
    config = dashboard_config(
        environment="production",
        allow_in_production=True,
    )
    app = create_dashboard_app(
        seeded_store(),
        config=config,
        authorization_dependencies=(require_admin,),
        max_page_size=2,
    )

    with TestClient(app) as client:
        unauthorized_list = client.get("/api/traces")
        unauthorized_detail = client.get("/api/traces/trace-1")
        unauthorized_clear = client.delete("/api/traces")
        authorized = client.get(
            "/api/traces",
            headers={"x-admin": "allowed"},
            params={"limit": 2},
        )

    for response in (
        unauthorized_list,
        unauthorized_detail,
        unauthorized_clear,
    ):
        assert response.status_code == 401
        assert "trace-1" not in response.text
        assert_security_headers(response)
    assert authorized.status_code == 200


def test_cookie_authenticated_mutation_requires_double_submit_csrf() -> None:
    store = seeded_store()
    app = create_dashboard_app(
        store,
        config=dashboard_config(),
        max_page_size=2,
        cookie_authenticated=True,
    )

    with TestClient(app) as client:
        client.cookies.set("fastapi_lens_csrf", "csrf-token")
        missing = client.delete("/api/traces")
        mismatch = client.delete(
            "/api/traces",
            headers={"x-fastapi-lens-csrf": "wrong"},
        )
        valid = client.delete(
            "/api/traces",
            headers={"x-fastapi-lens-csrf": "csrf-token"},
        )

    assert missing.status_code == 403
    assert mismatch.status_code == 403
    assert missing.json() == {"detail": "Mutation authorization failed."}
    assert valid.status_code == 200
    assert_security_headers(missing)


def test_dashboard_creation_fails_closed_for_disabled_or_unsafe_config() -> None:
    store = seeded_store()

    with pytest.raises(
        ValueError,
        match=r"Cannot create a dashboard app while it is disabled\.",
    ):
        create_dashboard_app(store, config=LensConfig())

    with pytest.raises(
        DashboardSecurityError,
        match=r"require authorization",
    ):
        create_dashboard_app(
            store,
            config=dashboard_config(
                environment="staging",
                allow_in_production=True,
            ),
        )

    with pytest.raises(
        ValueError,
        match=r"max_page_size must be greater than zero\.",
    ):
        create_dashboard_app(
            store,
            config=dashboard_config(),
            max_page_size=0,
        )

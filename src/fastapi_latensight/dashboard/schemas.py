"""Versioned dashboard API response schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DashboardSchema(BaseModel):
    """Strict immutable base for dashboard responses."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TraceListItem(DashboardSchema):
    trace_id: str
    method: str
    route: str | None
    path: str
    status_code: int | None
    started_at: datetime
    response_complete_duration_ms: float | None
    post_response_duration_ms: float | None
    sql_query_count: int
    dependency_count: int
    diagnostic_count: int
    complete: bool


class TraceListResponse(DashboardSchema):
    schema_version: str
    process_local: bool
    limit: int
    offset: int
    items: list[TraceListItem]


class TraceDetailResponse(DashboardSchema):
    schema_version: str
    process_local: bool
    trace: dict[str, Any]


class RouteSummaryItem(DashboardSchema):
    route: str
    request_count: int
    complete_count: int
    average_ms: float | None
    minimum_ms: float | None
    maximum_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    error_count: int


class RouteSummaryResponse(DashboardSchema):
    schema_version: str
    process_local: bool
    items: list[RouteSummaryItem]


class ClearTracesResponse(DashboardSchema):
    schema_version: str
    process_local: bool
    cleared_count: int

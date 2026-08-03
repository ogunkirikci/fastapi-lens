"""Secured, versioned dashboard JSON API."""

import math
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.params import Depends as DependsParameter
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.datastructures import MutableHeaders
from starlette.responses import FileResponse, HTMLResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_latensight.config import LatensightConfig
from fastapi_latensight.dashboard.schemas import (
    ClearTracesResponse,
    RouteSummaryItem,
    RouteSummaryResponse,
    TraceDetailResponse,
    TraceListItem,
    TraceListResponse,
)
from fastapi_latensight.exporters.json import (
    SUPPORTED_SCHEMA_VERSION,
    trace_snapshot_to_dict,
)
from fastapi_latensight.models import RequestTraceSnapshot, SegmentType
from fastapi_latensight.security import (
    AuthorizationDependency,
    CsrfPolicy,
    CsrfValidationError,
    dashboard_security_headers,
    validate_dashboard_configuration,
)
from fastapi_latensight.storage.base import TraceStore

_DASHBOARD_DIRECTORY = Path(__file__).parent
_TEMPLATE_DIRECTORY = _DASHBOARD_DIRECTORY / "templates"
_STATIC_DIRECTORY = _DASHBOARD_DIRECTORY / "static"
_STATIC_ASSETS = {
    "app.css": ("text/css", _STATIC_DIRECTORY / "app.css"),
    "app.js": ("text/javascript", _STATIC_DIRECTORY / "app.js"),
}


class DashboardSecurityHeadersMiddleware:
    """Apply dashboard browser and cache headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in dashboard_security_headers().items():
                    headers[name] = value
            await send(message)

        await self._app(scope, receive, wrapped_send)


def _fastapi_dependencies(
    dependencies: Sequence[AuthorizationDependency],
) -> list[DependsParameter]:
    return [
        dependency if isinstance(dependency, DependsParameter) else Depends(dependency)
        for dependency in dependencies
    ]


def _process_local(store: TraceStore) -> bool:
    return bool(getattr(store, "process_local", False))


def _template_environment() -> Environment:
    """Create an autoescaping environment for the packaged dashboard templates."""
    return Environment(
        loader=FileSystemLoader(_TEMPLATE_DIRECTORY),
        autoescape=select_autoescape(
            enabled_extensions=("html", "xml"),
            default_for_string=True,
            default=True,
        ),
    )


def _list_item(trace: RequestTraceSnapshot) -> TraceListItem:
    return TraceListItem(
        trace_id=trace.id,
        method=trace.method,
        route=trace.route,
        path=trace.path,
        status_code=trace.status_code,
        started_at=trace.started_at,
        response_complete_duration_ms=trace.response_complete_duration_ms,
        post_response_duration_ms=trace.post_response_duration_ms,
        sql_query_count=sum(
            segment.type is SegmentType.SQL for segment in trace.segments
        ),
        dependency_count=len(trace.logical_dependencies),
        diagnostic_count=len(trace.diagnostics),
        complete=trace.complete,
    )


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def _route_summaries(
    traces: Sequence[RequestTraceSnapshot],
) -> list[RouteSummaryItem]:
    groups: dict[str, list[RequestTraceSnapshot]] = defaultdict(list)
    for trace in traces:
        groups[trace.route or trace.path].append(trace)

    summaries = []
    for route, route_traces in sorted(groups.items()):
        durations = [
            duration
            for trace in route_traces
            if trace.complete
            for duration in [trace.response_complete_duration_ms]
            if duration is not None
        ]
        summaries.append(
            RouteSummaryItem(
                route=route,
                request_count=len(route_traces),
                complete_count=len(durations),
                average_ms=(sum(durations) / len(durations) if durations else None),
                minimum_ms=min(durations) if durations else None,
                maximum_ms=max(durations) if durations else None,
                p50_ms=_percentile(durations, 50),
                p95_ms=_percentile(durations, 95),
                p99_ms=_percentile(durations, 99),
                error_count=sum(
                    trace.error is not None
                    or (
                        trace.status_code is not None
                        and trace.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
                    for trace in route_traces
                ),
            )
        )
    return summaries


async def _all_traces(
    store: TraceStore,
    *,
    page_size: int,
) -> list[RequestTraceSnapshot]:
    traces: list[RequestTraceSnapshot] = []
    offset = 0
    while True:
        page = await store.list(limit=page_size, offset=offset)
        traces.extend(page)
        if len(page) < page_size:
            return traces
        offset += len(page)


def create_dashboard_app(
    store: TraceStore,
    *,
    config: LatensightConfig,
    authorization_dependencies: Sequence[AuthorizationDependency] = (),
    max_page_size: int = 200,
    csrf_policy: CsrfPolicy | None = None,
    cookie_authenticated: bool = False,
) -> FastAPI:
    """Create a mountable dashboard API with fail-closed security policy."""
    if not config.dashboard_enabled:
        raise ValueError("Cannot create a dashboard app while it is disabled.")
    if max_page_size <= 0:
        raise ValueError("max_page_size must be greater than zero.")
    validate_dashboard_configuration(
        config,
        authorization_dependencies=authorization_dependencies,
    )
    selected_csrf_policy = csrf_policy if csrf_policy is not None else CsrfPolicy()
    process_local = _process_local(store)
    templates = _template_environment()
    app = FastAPI(
        title="fastapi-latensight dashboard API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=_fastapi_dependencies(authorization_dependencies),
    )
    app.add_middleware(DashboardSecurityHeadersMiddleware)

    @app.get("/", response_class=HTMLResponse, name="dashboard_index")
    async def dashboard_index(request: Request) -> HTMLResponse:
        template = templates.get_template("index.html")
        return HTMLResponse(
            template.render(
                request=request,
                process_local=process_local,
                cookie_authenticated=cookie_authenticated,
                csrf_cookie_name=selected_csrf_policy.cookie_name,
                csrf_header_name=selected_csrf_policy.header_name,
            )
        )

    @app.get(
        "/static/{asset_name}",
        response_class=FileResponse,
        name="dashboard_asset",
    )
    async def dashboard_asset(asset_name: str) -> FileResponse:
        asset = _STATIC_ASSETS.get(asset_name)
        if asset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Dashboard asset not found.",
            )
        media_type, path = asset
        return FileResponse(path, media_type=media_type)

    @app.get("/api/traces", response_model=TraceListResponse)
    async def list_traces(
        limit: int = Query(default=100, ge=1, le=max_page_size),
        offset: int = Query(default=0, ge=0),
    ) -> TraceListResponse:
        try:
            traces = await store.list(limit=limit, offset=offset)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        return TraceListResponse(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            process_local=process_local,
            limit=limit,
            offset=offset,
            items=[_list_item(trace) for trace in traces],
        )

    @app.get("/api/traces/{trace_id}", response_model=TraceDetailResponse)
    async def trace_detail(trace_id: str) -> TraceDetailResponse:
        trace = await store.get(trace_id)
        if trace is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Trace not found.",
            )
        return TraceDetailResponse(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            process_local=process_local,
            trace=trace_snapshot_to_dict(trace),
        )

    @app.get("/api/routes", response_model=RouteSummaryResponse)
    async def route_summary() -> RouteSummaryResponse:
        try:
            traces = await _all_traces(store, page_size=max_page_size)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        return RouteSummaryResponse(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            process_local=process_local,
            items=_route_summaries(traces),
        )

    @app.delete("/api/traces", response_model=ClearTracesResponse)
    async def clear_traces(request: Request) -> ClearTracesResponse:
        try:
            selected_csrf_policy.validate(
                method=request.method,
                headers=request.headers,
                cookies=request.cookies,
                cookie_authenticated=cookie_authenticated,
            )
        except CsrfValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Mutation authorization failed.",
            ) from error
        traces = await _all_traces(store, page_size=max_page_size)
        await store.clear()
        return ClearTracesResponse(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            process_local=process_local,
            cleared_count=len(traces),
        )

    return app

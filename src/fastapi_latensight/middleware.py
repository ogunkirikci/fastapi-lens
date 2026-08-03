"""Pure ASGI request lifecycle tracing middleware."""

from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from threading import RLock
from time import perf_counter_ns
from typing import Any
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_latensight.collector import TraceCollector
from fastapi_latensight.context import bind_request_context, reset_request_context
from fastapi_latensight.diagnostics.base import DiagnosticRule
from fastapi_latensight.models import RequestTrace, TraceError
from fastapi_latensight.redaction import TraceSanitizer
from fastapi_latensight.storage.base import TraceStore
from fastapi_latensight.storage.memory import MemoryTraceStore
from fastapi_latensight.utils.patterns import RouteFilter


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ProfilerState:
    """Thread-safe runtime enablement state for new request traces."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._lock = RLock()

    @property
    def enabled(self) -> bool:
        """Return whether new requests should create traces."""
        with self._lock:
            return self._enabled

    def enable(self) -> None:
        """Enable tracing for requests that start after this call."""
        with self._lock:
            self._enabled = True

    def disable(self) -> None:
        """Disable tracing for requests that start after this call."""
        with self._lock:
            self._enabled = False


class LatensightMiddleware:
    """Collect request lifecycle traces without BaseHTTPMiddleware."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: TraceStore | None = None,
        state: ProfilerState | None = None,
        include_routes: Sequence[str] = ("*",),
        exclude_routes: Sequence[str] = (),
        diagnostic_rules: Sequence[DiagnosticRule] = (),
        sanitizer: TraceSanitizer | None = None,
        clock_ns: Callable[[], int] = perf_counter_ns,
        wall_clock: Callable[[], datetime] = _utc_now,
        trace_id_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._app = app
        self.store = store if store is not None else MemoryTraceStore()
        self.state = state if state is not None else ProfilerState()
        self._route_filter = RouteFilter(
            include=include_routes,
            exclude=exclude_routes,
        )
        self._diagnostic_rules = tuple(diagnostic_rules)
        self._sanitizer = sanitizer if sanitizer is not None else TraceSanitizer()
        self._clock_ns = clock_ns
        self._wall_clock = wall_clock
        self._trace_id_factory = trace_id_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not self.state.enabled or not self._route_filter.allows_raw_path(path):
            await self._app(scope, receive, send)
            return
        base_root_path = scope.get("root_path", "")
        if not isinstance(base_root_path, str):
            base_root_path = ""

        trace = RequestTrace(
            schema_version="1.0",
            id=self._trace_id_factory(),
            method=scope.get("method", ""),
            path=path,
            started_at=self._wall_clock(),
            request_received_ns=self._clock_ns(),
        )
        collector = TraceCollector(trace)
        context_token = bind_request_context(collector)

        async def wrapped_receive() -> Message:
            return await receive()

        async def wrapped_send(message: Message) -> None:
            message_type = message["type"]
            if (
                message_type == "http.response.start"
                and trace.response_started_ns is None
            ):
                trace.response_started_ns = self._clock_ns()
                trace.status_code = message["status"]
            if message_type == "http.response.body":
                has_more_body = message.get("more_body", False)
                await send(message)
                if not has_more_body and trace.response_body_completed_ns is None:
                    trace.response_body_completed_ns = self._clock_ns()
                return
            await send(message)

        try:
            try:
                await self._app(scope, wrapped_receive, wrapped_send)
            except BaseException as error:
                trace.error = TraceError(
                    type=type(error).__name__,
                    message=str(error),
                )
                raise
            finally:
                trace.application_completed_ns = self._clock_ns()
                trace.complete = trace.response_body_completed_ns is not None
                trace.route = self._route_template(
                    scope,
                    base_root_path=base_root_path,
                )
                await self._finalize(
                    collector,
                    route_allowed=self._route_filter.allows_route(
                        trace.route,
                        trace.path,
                    ),
                )
        finally:
            reset_request_context(context_token)

    async def _finalize(
        self,
        collector: TraceCollector,
        *,
        route_allowed: bool,
    ) -> None:
        try:
            trace = collector.trace
            if route_allowed:
                lifecycle_snapshot = trace.snapshot()
                for rule in self._diagnostic_rules:
                    try:
                        trace.diagnostics.extend(rule.evaluate(lifecycle_snapshot))
                    except Exception:
                        continue
                self._sanitizer.sanitize(trace)
            snapshot = collector.finalize()
            if route_allowed:
                with suppress(Exception):
                    await self.store.save(snapshot)
        except Exception:
            pass

    @staticmethod
    def _route_template(
        scope: Scope,
        *,
        base_root_path: str,
    ) -> str | None:
        route_object: Any = scope.get("route")
        route_path = getattr(route_object, "path", None)
        if not isinstance(route_path, str):
            return None

        root_path = scope.get("root_path", "")
        if not isinstance(root_path, str):
            return route_path
        mount_prefix = (
            root_path[len(base_root_path) :]
            if root_path.startswith(base_root_path)
            else root_path
        )
        mount_prefix = mount_prefix.rstrip("/")
        if (
            mount_prefix
            and route_path != mount_prefix
            and not route_path.startswith(f"{mount_prefix}/")
        ):
            return f"{mount_prefix}{route_path}"
        return route_path

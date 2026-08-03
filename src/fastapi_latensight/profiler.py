"""Public FastAPI application orchestration."""

import os
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI

from fastapi_latensight.config import LatensightConfig, enabled_from_environment
from fastapi_latensight.dashboard import create_dashboard_app
from fastapi_latensight.diagnostics import DiagnosticConfig, DiagnosticEngine
from fastapi_latensight.instrumentation import (
    dependency_instrumentation,
    handler_instrumentation,
    serialization_instrumentation,
)
from fastapi_latensight.middleware import LatensightMiddleware, ProfilerState
from fastapi_latensight.models import RequestTraceSnapshot
from fastapi_latensight.redaction import TraceSanitizer, TraceSanitizerConfig
from fastapi_latensight.security import AuthorizationDependency, CsrfPolicy
from fastapi_latensight.storage.base import TraceStore
from fastapi_latensight.storage.memory import MemoryTraceStore


class Latensight:
    """Attach profiling, storage, diagnostics, and an optional dashboard."""

    def __init__(
        self,
        app: FastAPI,
        *,
        config: LatensightConfig | None = None,
        store: TraceStore | None = None,
        dashboard_dependencies: Sequence[AuthorizationDependency] = (),
        cookie_authenticated_dashboard: bool = False,
        csrf_policy: CsrfPolicy | None = None,
    ) -> None:
        if app.middleware_stack is not None:
            raise RuntimeError(
                "Latensight must be attached before application startup."
            )
        if getattr(app.state, "fastapi_latensight", None) is not None:
            raise RuntimeError("A Latensight instance is already attached to this app.")

        self.app = app
        self.config = self._resolve_config(config)
        self.store = (
            store
            if store is not None
            else MemoryTraceStore(
                max_traces=self.config.max_traces,
                max_page_size=self.config.max_api_page_size,
                max_trace_bytes=self.config.max_trace_bytes,
            )
        )
        self.state = ProfilerState(enabled=self.config.enabled)
        self._owner = object()
        self._closed = False
        self._registered_engines: dict[int, Any] = {}

        dashboard_app = (
            create_dashboard_app(
                self.store,
                config=self.config,
                authorization_dependencies=dashboard_dependencies,
                max_page_size=self.config.max_api_page_size,
                csrf_policy=csrf_policy,
                cookie_authenticated=cookie_authenticated_dashboard,
            )
            if self.config.dashboard_enabled
            else None
        )
        diagnostic_engine = DiagnosticEngine(
            config=DiagnosticConfig(
                slow_request_threshold_ms=self.config.slow_request_threshold_ms,
                slow_dependency_threshold_ms=(self.config.slow_dependency_threshold_ms),
            )
        )
        sanitizer = TraceSanitizer(
            TraceSanitizerConfig(
                max_segments_per_trace=self.config.max_segments_per_trace,
                max_attribute_length=self.config.max_attribute_length,
                max_error_length=self.config.max_error_length,
                max_trace_bytes=self.config.max_trace_bytes,
            )
        )
        excluded_routes = self._excluded_routes()

        installed_adapters: list[Any] = []
        try:
            handler_instrumentation.install(self._owner)
            installed_adapters.append(handler_instrumentation)
            if self.config.capture_dependencies:
                dependency_instrumentation.install(self._owner)
                installed_adapters.append(dependency_instrumentation)
            if self.config.capture_serialization:
                serialization_instrumentation.install(self._owner)
                installed_adapters.append(serialization_instrumentation)
            app.add_middleware(
                LatensightMiddleware,
                store=self.store,
                state=self.state,
                include_routes=self.config.include_routes,
                exclude_routes=excluded_routes,
                diagnostic_rules=(diagnostic_engine,),
                sanitizer=sanitizer,
            )
            if dashboard_app is not None:
                app.mount(
                    self.config.dashboard_path,
                    dashboard_app,
                )
        except Exception:
            for adapter in reversed(installed_adapters):
                adapter.remove(self._owner)
            raise
        app.state.fastapi_latensight = self

    @staticmethod
    def _resolve_config(config: LatensightConfig | None) -> LatensightConfig:
        if config is not None:
            return config
        environment_enabled = enabled_from_environment(os.environ)
        return LatensightConfig(
            enabled=True if environment_enabled is None else environment_enabled
        )

    def _excluded_routes(self) -> tuple[str, ...]:
        dashboard_pattern = f"{self.config.dashboard_path.rstrip('/')}*"
        if dashboard_pattern in self.config.exclude_routes:
            return self.config.exclude_routes
        return (*self.config.exclude_routes, dashboard_pattern)

    @property
    def enabled(self) -> bool:
        """Return whether new requests are currently traced."""
        return self.state.enabled

    def enable(self) -> None:
        """Enable tracing for requests that start after this call."""
        self._ensure_open()
        self.state.enable()

    def disable(self) -> None:
        """Stop creating traces for new requests."""
        self.state.disable()

    def instrument_sqlalchemy(self, engine: Any) -> None:
        """Capture statements from one explicitly registered SQLAlchemy engine."""
        self._ensure_open()
        if not self.config.capture_sql:
            raise RuntimeError(
                "SQL capture is disabled; set LatensightConfig(capture_sql=True)."
            )
        from fastapi_latensight.instrumentation.sqlalchemy import (
            sqlalchemy_instrumentation,
        )

        sqlalchemy_instrumentation.register(
            engine,
            self._owner,
            max_sql_length=self.config.max_sql_length,
        )
        self._registered_engines[id(engine)] = engine

    def uninstrument_sqlalchemy(self, engine: Any) -> None:
        """Release this Latensight instance's registration for one engine."""
        from fastapi_latensight.instrumentation.sqlalchemy import (
            sqlalchemy_instrumentation,
        )

        sqlalchemy_instrumentation.unregister(engine, self._owner)
        self._registered_engines.pop(id(engine), None)

    async def clear_traces(self) -> None:
        """Remove every trace from the configured store."""
        await self.store.clear()

    async def get_trace(self, trace_id: str) -> RequestTraceSnapshot | None:
        """Return one immutable trace snapshot when present."""
        return await self.store.get(trace_id)

    async def list_traces(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RequestTraceSnapshot]:
        """Return a stable page of immutable trace snapshots."""
        return await self.store.list(limit=limit, offset=offset)

    def close(self) -> None:
        """Disable tracing and release every process-global instrumentation owner."""
        if self._closed:
            return
        self.state.disable()
        if self._registered_engines:
            from fastapi_latensight.instrumentation.sqlalchemy import (
                sqlalchemy_instrumentation,
            )

            sqlalchemy_instrumentation.unregister_owner(self._owner)
            self._registered_engines.clear()
        serialization_instrumentation.remove(self._owner)
        dependency_instrumentation.remove(self._owner)
        handler_instrumentation.remove(self._owner)
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Latensight is closed.")

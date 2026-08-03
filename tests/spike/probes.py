from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from functools import wraps
from threading import Lock
from time import perf_counter_ns
from types import ModuleType
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

AsyncCallable = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class LifecycleObservation:
    request_received_ns: int
    response_started_ns: int | None = None
    response_body_completed_ns: int | None = None
    application_completed_ns: int | None = None
    status_code: int | None = None
    body_events: list[bool] = field(default_factory=list)
    error_type: str | None = None

    @property
    def complete(self) -> bool:
        return (
            self.response_body_completed_ns is not None
            and self.application_completed_ns is not None
        )


class LifecycleProbeMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        observations: list[LifecycleObservation],
        event_log: list[str] | None = None,
    ) -> None:
        self._app = app
        self._observations = observations
        self._event_log = event_log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        observation = LifecycleObservation(request_received_ns=perf_counter_ns())
        self._observations.append(observation)

        async def wrapped_send(message: Message) -> None:
            message_type = message["type"]
            if message_type == "http.response.start":
                observation.response_started_ns = perf_counter_ns()
                observation.status_code = message["status"]
                if self._event_log is not None:
                    self._event_log.append("response.start")
            elif message_type == "http.response.body":
                has_more_body = message.get("more_body", False)
                observation.body_events.append(has_more_body)
                await send(message)
                if not has_more_body:
                    observation.response_body_completed_ns = perf_counter_ns()
                    if self._event_log is not None:
                        self._event_log.append("response.body.complete")
                return
            await send(message)

        try:
            await self._app(scope, receive, wrapped_send)
        except BaseException as error:
            observation.error_type = type(error).__name__
            raise
        finally:
            observation.application_completed_ns = perf_counter_ns()
            if self._event_log is not None:
                self._event_log.append("application.complete")


@dataclass(slots=True, frozen=True)
class DependencyCallEvent:
    name: str
    started_ns: int
    ended_ns: int


class RouteDependencyWrapperProbe:
    """Prototype that demonstrates route-tree wrapping tradeoffs.

    This deliberately supports regular sync and async dependency callables only.
    Generator wrapping requires exception injection and teardown preservation,
    which is one reason this approach is not selected for production.
    """

    def __init__(self) -> None:
        self.events: list[DependencyCallEvent] = []
        self._wrappers: dict[Callable[..., Any], Callable[..., Any]] = {}
        self._originals: list[tuple[Dependant, Callable[..., Any]]] = []

    def install(self, app: FastAPI) -> None:
        for route in app.routes:
            if isinstance(route, APIRoute):
                for dependency in route.dependant.dependencies:
                    self._install_dependency(dependency)

    def restore(self) -> None:
        for dependency, original in reversed(self._originals):
            dependency.call = original
        self._originals.clear()
        self._wrappers.clear()

    def _install_dependency(self, dependency: Dependant) -> None:
        original = dependency.call
        if original is None:
            return
        if inspect.isgeneratorfunction(original) or inspect.isasyncgenfunction(
            original
        ):
            raise TypeError("Generator dependencies are intentionally unsupported")

        wrapper = self._wrappers.get(original)
        if wrapper is None:
            wrapper = self._make_wrapper(original)
            self._wrappers[original] = wrapper
        self._originals.append((dependency, original))
        dependency.call = wrapper

        for child in dependency.dependencies:
            self._install_dependency(child)

    def _make_wrapper(self, original: Callable[..., Any]) -> Callable[..., Any]:
        name = getattr(original, "__name__", type(original).__name__)
        if inspect.iscoroutinefunction(original):

            @wraps(original)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                started_ns = perf_counter_ns()
                try:
                    return await original(*args, **kwargs)
                finally:
                    self.events.append(
                        DependencyCallEvent(name, started_ns, perf_counter_ns())
                    )

            return async_wrapper

        @wraps(original)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            started_ns = perf_counter_ns()
            try:
                return original(*args, **kwargs)
            finally:
                self.events.append(
                    DependencyCallEvent(name, started_ns, perf_counter_ns())
                )

        return sync_wrapper


class ReferenceCountedAsyncPatch:
    """Reference-counted prototype for process-global async function patches."""

    def __init__(
        self,
        module: ModuleType,
        attribute: str,
        events: list[str],
    ) -> None:
        self._module = module
        self._attribute = attribute
        self._events = events
        self._owners: set[object] = set()
        self._lock = Lock()
        original = getattr(module, attribute)
        if not callable(original):
            raise TypeError(f"{module.__name__}.{attribute} is not callable")
        self._original: AsyncCallable = original
        self._wrapper = self._make_wrapper()

    @property
    def installed(self) -> bool:
        return getattr(self._module, self._attribute) is self._wrapper

    def install(self, owner: object) -> None:
        with self._lock:
            if owner in self._owners:
                return
            if not self._owners:
                setattr(self._module, self._attribute, self._wrapper)
            self._owners.add(owner)

    def remove(self, owner: object) -> None:
        with self._lock:
            self._owners.discard(owner)
            if not self._owners and self.installed:
                setattr(self._module, self._attribute, self._original)

    def restore_all(self) -> None:
        with self._lock:
            self._owners.clear()
            if self.installed:
                setattr(self._module, self._attribute, self._original)

    def _make_wrapper(self) -> AsyncCallable:
        @wraps(self._original)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._events.append(f"{self._attribute}.start")
            try:
                return await self._original(*args, **kwargs)
            finally:
                self._events.append(f"{self._attribute}.end")

        return wrapper


@dataclass(slots=True, frozen=True)
class SqlEvent:
    statement: str
    duration_ns: int
    failed: bool


_active_sql_events: ContextVar[list[SqlEvent] | None] = ContextVar(
    "fastapi_latensight_spike_sql_events",
    default=None,
)


class SqlCapture:
    def __init__(self) -> None:
        self._targets: dict[int, tuple[Engine, int]] = {}
        self._stack_key = f"fastapi_latensight_spike_{id(self)}"
        self._lock = Lock()

    def register(self, engine: Engine | AsyncEngine) -> None:
        target = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
        target_id = id(target)
        with self._lock:
            current = self._targets.get(target_id)
            if current is not None:
                self._targets[target_id] = (target, current[1] + 1)
                return
            event.listen(target, "before_cursor_execute", self._before)
            event.listen(target, "after_cursor_execute", self._after)
            event.listen(target, "handle_error", self._error)
            self._targets[target_id] = (target, 1)

    def unregister(self, engine: Engine | AsyncEngine) -> None:
        target = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
        target_id = id(target)
        with self._lock:
            current = self._targets.get(target_id)
            if current is None:
                return
            if current[1] > 1:
                self._targets[target_id] = (target, current[1] - 1)
                return
            event.remove(target, "before_cursor_execute", self._before)
            event.remove(target, "after_cursor_execute", self._after)
            event.remove(target, "handle_error", self._error)
            del self._targets[target_id]

    def start(self) -> tuple[list[SqlEvent], Token[list[SqlEvent] | None]]:
        events: list[SqlEvent] = []
        return events, _active_sql_events.set(events)

    def stop(self, token: Token[list[SqlEvent] | None]) -> None:
        _active_sql_events.reset(token)

    def _before(
        self,
        connection: Connection,
        _cursor: Any,
        _statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if _active_sql_events.get() is None:
            return
        stack = self._stack(connection.info)
        stack.append(perf_counter_ns())

    def _after(
        self,
        connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        events = _active_sql_events.get()
        if events is None:
            return
        started_ns = self._pop_start(connection.info)
        if started_ns is not None:
            events.append(SqlEvent(statement, perf_counter_ns() - started_ns, False))

    def _error(self, context: Any) -> None:
        if getattr(context, "is_pre_ping", False):
            return
        events = _active_sql_events.get()
        connection = getattr(context, "connection", None)
        if events is None or connection is None:
            return
        started_ns = self._pop_start(connection.info)
        if started_ns is not None:
            statement = getattr(context, "statement", None) or "<unknown>"
            events.append(SqlEvent(statement, perf_counter_ns() - started_ns, True))

    def _stack(self, info: MutableMapping[str, Any]) -> list[int]:
        value = info.setdefault(self._stack_key, [])
        if not isinstance(value, list):
            raise TypeError("SQL timing stack has an invalid type")
        return value

    def _pop_start(self, info: MutableMapping[str, Any]) -> int | None:
        stack = self._stack(info)
        return stack.pop() if stack else None

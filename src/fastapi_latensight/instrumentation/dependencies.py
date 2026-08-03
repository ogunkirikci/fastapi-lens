"""Versioned FastAPI dependency solver instrumentation."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from threading import RLock
from time import perf_counter_ns
from types import TracebackType
from typing import Any, cast
from uuid import uuid4

import fastapi
import fastapi.routing
from fastapi.dependencies import utils as dependency_utils
from fastapi.dependencies.models import Dependant
from fastapi.security import SecurityScopes
from starlette.background import BackgroundTasks
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.websockets import WebSocket

from fastapi_latensight.collector import TraceCollector
from fastapi_latensight.context import current_collector, enter_segment, exit_segment
from fastapi_latensight.models import (
    DependencyCacheStatus,
    DependencyScope,
    JsonValue,
    LogicalDependencyNode,
    SegmentStatus,
    SegmentType,
    TraceError,
    TraceSegment,
)

DependencyCacheKey = tuple[Callable[..., Any] | None, tuple[str, ...], str]
DependencySolver = Callable[..., Awaitable[Any]]
_routing: Any = fastapi.routing
_utils: Any = dependency_utils

_BASE_PARAMETERS = {
    "request",
    "dependant",
    "body",
    "background_tasks",
    "response",
    "dependency_overrides_provider",
    "dependency_cache",
    "async_exit_stack",
    "embed_body_fields",
}
_OPTIONAL_PARAMETERS = {"_uses_scopes_cache"}


@dataclass(slots=True)
class _DependencyTraceState:
    collector: TraceCollector
    cache_sources: dict[DependencyCacheKey, str] = field(default_factory=dict)


def _callable_name(call: Callable[..., Any] | None) -> str:
    if call is None:
        return "<missing dependency>"
    return getattr(call, "__name__", type(call).__name__)


def _status_for_error(error: BaseException | None) -> SegmentStatus:
    if error is None:
        return SegmentStatus.OK
    if isinstance(error, asyncio.CancelledError):
        return SegmentStatus.CANCELLED
    return SegmentStatus.ERROR


def _trace_error(error: BaseException | None) -> TraceError | None:
    if error is None:
        return None
    return TraceError(type=type(error).__name__, message=str(error))


class _InstrumentedDependencyContext(AbstractAsyncContextManager[Any]):
    def __init__(
        self,
        context_manager: AbstractAsyncContextManager[Any],
        *,
        state: _DependencyTraceState,
        node: LogicalDependencyNode,
        name: str,
        attributes: dict[str, JsonValue],
    ) -> None:
        self._context_manager = context_manager
        self._state = state
        self._node = node
        self._name = name
        self._attributes = attributes

    async def __aenter__(self) -> Any:
        return await self._measure(
            SegmentType.DEPENDENCY_SETUP,
            self._context_manager.__aenter__,
        )

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        result = await self._measure(
            SegmentType.DEPENDENCY_CLEANUP,
            lambda: self._context_manager.__aexit__(
                error_type,
                error,
                traceback,
            ),
        )
        return cast(bool | None, result)

    async def _measure(
        self,
        segment_type: SegmentType,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        segment = TraceSegment(
            id=uuid4().hex,
            trace_id=self._state.collector.trace.id,
            type=segment_type,
            name=self._name,
            start_ns=perf_counter_ns(),
            logical_dependency_id=self._node.id,
            attributes=self._attributes,
        )
        if segment_type is SegmentType.DEPENDENCY_SETUP:
            self._node.setup_segment_id = segment.id
        else:
            self._node.cleanup_segment_id = segment.id
        token = enter_segment(segment)
        caught: BaseException | None = None
        try:
            return await operation()
        except BaseException as error:
            caught = error
            raise
        finally:
            self._state.collector.finish_segment(
                segment,
                end_ns=perf_counter_ns(),
                status=_status_for_error(caught),
                error=_trace_error(caught),
            )
            exit_segment(token)


class DependencyInstrumentation:
    """Install a version-aware dependency solver with shared ownership."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._owners: set[object] = set()
        self._original_utils: DependencySolver = dependency_utils.solve_dependencies
        self._original_routing: DependencySolver = _routing.solve_dependencies
        self._uses_scopes_cache = (
            "_uses_scopes_cache" in inspect.signature(self._original_utils).parameters
        )
        self._wrapper = self._make_wrapper()

    @property
    def installed(self) -> bool:
        """Return whether both FastAPI solver references use this adapter."""
        with self._lock:
            return (
                dependency_utils.solve_dependencies is self._wrapper
                and _routing.solve_dependencies is self._wrapper
            )

    def install(self, owner: object) -> None:
        """Install the adapter for one owner after compatibility checks."""
        with self._lock:
            if owner in self._owners:
                return
            if not self._owners:
                self._validate_compatibility()
                if (
                    dependency_utils.solve_dependencies is not self._original_utils
                    or _routing.solve_dependencies is not self._original_routing
                ):
                    raise RuntimeError(
                        "FastAPI dependency instrumentation targets were replaced."
                    )
                dependency_utils.solve_dependencies = cast(Any, self._wrapper)
                _routing.solve_dependencies = self._wrapper
            self._owners.add(owner)

    def remove(self, owner: object) -> None:
        """Remove one owner and restore both references after the last owner."""
        with self._lock:
            self._owners.discard(owner)
            if not self._owners:
                self._restore()

    def restore_all(self) -> None:
        """Remove every owner and restore the exact original solver references."""
        with self._lock:
            self._owners.clear()
            self._restore()

    def _restore(self) -> None:
        if dependency_utils.solve_dependencies is self._wrapper:
            dependency_utils.solve_dependencies = cast(Any, self._original_utils)
        if _routing.solve_dependencies is self._wrapper:
            _routing.solve_dependencies = self._original_routing

    def _validate_compatibility(self) -> None:
        version = tuple(int(part) for part in fastapi.__version__.split(".")[:3])
        if not (version >= (0, 121, 0) and version < (0, 142, 0)):
            raise RuntimeError(
                f"FastAPI {fastapi.__version__} has no dependency adapter."
            )
        parameters = set(inspect.signature(self._original_utils).parameters)
        if (
            not _BASE_PARAMETERS.issubset(parameters)
            or parameters - _BASE_PARAMETERS - _OPTIONAL_PARAMETERS
        ):
            raise RuntimeError(
                "FastAPI solve_dependencies has an unsupported signature."
            )
        try:
            source = inspect.getsource(self._original_utils)
        except (OSError, TypeError) as error:
            raise RuntimeError(
                "FastAPI solve_dependencies source cannot be verified."
            ) from error
        required_markers = (
            "for sub_dependant in dependant.dependencies:",
            "dependency_cache",
            "run_in_threadpool",
            "request_body_to_args",
        )
        if not all(marker in source for marker in required_markers):
            raise RuntimeError(
                "FastAPI solve_dependencies behavior guard did not match."
            )

    def _make_wrapper(self) -> DependencySolver:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            collector = current_collector()
            if collector is None or collector.finalized or args:
                return await self._original_utils(*args, **kwargs)
            state = _DependencyTraceState(collector=collector)
            return await self._solve(
                kwargs,
                state=state,
                parent_logical_id=None,
            )

        return wrapper

    async def _solve(
        self,
        arguments: dict[str, Any],
        *,
        state: _DependencyTraceState,
        parent_logical_id: str | None,
    ) -> Any:
        request = cast(Request | WebSocket, arguments["request"])
        dependant = cast(Dependant, arguments["dependant"])
        body = arguments.get("body")
        background_tasks = arguments.get("background_tasks")
        response = cast(Response | None, arguments.get("response"))
        overrides = arguments.get("dependency_overrides_provider")
        dependency_cache = cast(
            dict[DependencyCacheKey, Any] | None,
            arguments.get("dependency_cache"),
        )
        embed_body_fields = cast(bool, arguments["embed_body_fields"])
        uses_scopes_cache = arguments.get("_uses_scopes_cache")
        if self._uses_scopes_cache and uses_scopes_cache is None:
            uses_scopes_cache = {}
            arguments["_uses_scopes_cache"] = uses_scopes_cache

        request_stack = request.scope.get("fastapi_inner_astack")
        function_stack = request.scope.get("fastapi_function_astack")
        assert isinstance(request_stack, AsyncExitStack), (
            "fastapi_inner_astack not found in request scope"
        )
        assert isinstance(function_stack, AsyncExitStack), (
            "fastapi_function_astack not found in request scope"
        )

        values: dict[str, Any] = {}
        errors: list[Any] = []
        if response is None:
            response = Response()
            del response.headers["content-length"]
            response.status_code = None  # type: ignore[assignment]
        if dependency_cache is None:
            dependency_cache = {}

        for sub_dependant in dependant.dependencies:
            call = cast(Callable[..., Any], sub_dependant.call)
            use_sub_dependant = sub_dependant
            if overrides and overrides.dependency_overrides:
                original_call = sub_dependant.call
                call = cast(
                    Callable[..., Any],
                    getattr(overrides, "dependency_overrides", {}).get(
                        original_call,
                        original_call,
                    ),
                )
                use_sub_dependant = self._override_dependant(
                    sub_dependant,
                    call=call,
                )

            cache_key = self._cache_key(
                sub_dependant,
                uses_scopes_cache=uses_scopes_cache,
            )
            cache_status = self._cache_status(
                sub_dependant,
                cache_key=cache_key,
                dependency_cache=dependency_cache,
            )
            node = LogicalDependencyNode(
                id=uuid4().hex,
                trace_id=state.collector.trace.id,
                name=_callable_name(use_sub_dependant.call),
                cache_status=cache_status,
                parent_id=parent_logical_id,
                cached_from_id=self._cached_from(
                    cache_status,
                    cache_key=cache_key,
                    state=state,
                ),
                scope=self._dependency_scope(use_sub_dependant),
            )
            state.collector.add_logical_dependency(node)

            nested_arguments = dict(arguments)
            nested_arguments.update(
                dependant=use_sub_dependant,
                background_tasks=background_tasks,
                response=response,
                dependency_cache=dependency_cache,
            )
            solved_result = await self._solve(
                nested_arguments,
                state=state,
                parent_logical_id=node.id,
            )
            background_tasks = solved_result.background_tasks
            if solved_result.errors:
                errors.extend(solved_result.errors)
                continue

            cache_status = self._cache_status(
                sub_dependant,
                cache_key=cache_key,
                dependency_cache=dependency_cache,
            )
            node.cache_status = cache_status
            node.cached_from_id = self._cached_from(
                cache_status,
                cache_key=cache_key,
                state=state,
            )
            if cache_status is DependencyCacheStatus.HIT:
                solved = dependency_cache[cache_key]
            else:
                solved = await self._execute_dependency(
                    call,
                    use_sub_dependant=use_sub_dependant,
                    values=solved_result.values,
                    node=node,
                    state=state,
                    request_stack=request_stack,
                    function_stack=function_stack,
                )
            if sub_dependant.name is not None:
                values[sub_dependant.name] = solved
            if cache_key not in dependency_cache:
                dependency_cache[cache_key] = solved
                state.cache_sources[cache_key] = node.id

        path_values, path_errors = dependency_utils.request_params_to_args(
            dependant.path_params,
            request.path_params,
        )
        query_values, query_errors = dependency_utils.request_params_to_args(
            dependant.query_params,
            request.query_params,
        )
        header_values, header_errors = dependency_utils.request_params_to_args(
            dependant.header_params,
            request.headers,
        )
        cookie_values, cookie_errors = dependency_utils.request_params_to_args(
            dependant.cookie_params,
            request.cookies,
        )
        values.update(path_values)
        values.update(query_values)
        values.update(header_values)
        values.update(cookie_values)
        errors += path_errors + query_errors + header_errors + cookie_errors
        if dependant.body_params:
            body_values, body_errors = await dependency_utils.request_body_to_args(
                body_fields=dependant.body_params,
                received_body=body,
                embed_body_fields=embed_body_fields,
            )
            values.update(body_values)
            errors.extend(body_errors)
        if dependant.http_connection_param_name:
            values[dependant.http_connection_param_name] = request
        if dependant.request_param_name and isinstance(request, Request):
            values[dependant.request_param_name] = request
        elif dependant.websocket_param_name and isinstance(request, WebSocket):
            values[dependant.websocket_param_name] = request
        if dependant.background_tasks_param_name:
            if background_tasks is None:
                background_tasks = BackgroundTasks()
            values[dependant.background_tasks_param_name] = background_tasks
        if dependant.response_param_name:
            values[dependant.response_param_name] = response
        if dependant.security_scopes_param_name:
            values[dependant.security_scopes_param_name] = SecurityScopes(
                scopes=self._oauth_scopes(dependant),
            )
        return dependency_utils.SolvedDependency(
            values=values,
            errors=errors,
            background_tasks=background_tasks,
            response=response,
            dependency_cache=dependency_cache,
        )

    async def _execute_dependency(
        self,
        call: Callable[..., Any],
        *,
        use_sub_dependant: Dependant,
        values: dict[str, Any],
        node: LogicalDependencyNode,
        state: _DependencyTraceState,
        request_stack: AsyncExitStack,
        function_stack: AsyncExitStack,
    ) -> Any:
        attributes: dict[str, JsonValue] = {
            "cache_status": node.cache_status.value,
            "execution_mode": self._execution_mode(use_sub_dependant),
        }
        if node.scope is not None:
            attributes["scope"] = node.scope.value
        if self._is_generator(use_sub_dependant):
            context_manager = self._generator_context(
                use_sub_dependant,
                values=values,
            )
            instrumented = _InstrumentedDependencyContext(
                context_manager,
                state=state,
                node=node,
                name=_callable_name(use_sub_dependant.call),
                attributes=attributes,
            )
            stack = (
                function_stack
                if use_sub_dependant.scope == "function"
                else request_stack
            )
            return await stack.enter_async_context(instrumented)

        segment = TraceSegment(
            id=uuid4().hex,
            trace_id=state.collector.trace.id,
            type=SegmentType.DEPENDENCY_SETUP,
            name=_callable_name(use_sub_dependant.call),
            start_ns=perf_counter_ns(),
            logical_dependency_id=node.id,
            attributes=attributes,
        )
        node.setup_segment_id = segment.id
        token = enter_segment(segment)
        caught: BaseException | None = None
        try:
            if self._is_coroutine(use_sub_dependant):
                return await call(**values)
            return await run_in_threadpool(call, **values)
        except BaseException as error:
            caught = error
            raise
        finally:
            state.collector.finish_segment(
                segment,
                end_ns=perf_counter_ns(),
                status=_status_for_error(caught),
                error=_trace_error(caught),
            )
            exit_segment(token)

    @staticmethod
    def _cache_status(
        dependant: Dependant,
        *,
        cache_key: DependencyCacheKey,
        dependency_cache: dict[DependencyCacheKey, Any],
    ) -> DependencyCacheStatus:
        if not dependant.use_cache:
            return DependencyCacheStatus.BYPASS
        if cache_key in dependency_cache:
            return DependencyCacheStatus.HIT
        return DependencyCacheStatus.MISS

    @staticmethod
    def _cached_from(
        cache_status: DependencyCacheStatus,
        *,
        cache_key: DependencyCacheKey,
        state: _DependencyTraceState,
    ) -> str | None:
        if cache_status is not DependencyCacheStatus.HIT:
            return None
        return state.cache_sources.get(cache_key, f"external:{uuid4().hex}")

    @staticmethod
    def _dependency_scope(dependant: Dependant) -> DependencyScope | None:
        if not DependencyInstrumentation._is_generator(dependant):
            return None
        if dependant.scope == "function":
            return DependencyScope.FUNCTION
        return DependencyScope.REQUEST

    @staticmethod
    def _execution_mode(dependant: Dependant) -> str:
        if DependencyInstrumentation._is_generator(dependant):
            return (
                "async_generator"
                if DependencyInstrumentation._is_async_generator(dependant)
                else "sync_generator_thread_pool"
            )
        if DependencyInstrumentation._is_coroutine(dependant):
            return "async"
        return "thread_pool"

    @staticmethod
    def _is_generator(dependant: Dependant) -> bool:
        return DependencyInstrumentation._is_async_generator(
            dependant
        ) or DependencyInstrumentation._is_sync_generator(dependant)

    @staticmethod
    def _is_async_generator(dependant: Dependant) -> bool:
        detector = getattr(dependency_utils, "_is_async_gen_callable", None)
        if detector is not None:
            return bool(detector(dependant.call))
        return bool(cast(Any, dependant).is_async_gen_callable)

    @staticmethod
    def _is_sync_generator(dependant: Dependant) -> bool:
        detector = getattr(dependency_utils, "_is_gen_callable", None)
        if detector is not None:
            return bool(detector(dependant.call))
        return bool(cast(Any, dependant).is_gen_callable)

    @staticmethod
    def _is_coroutine(dependant: Dependant) -> bool:
        detector = getattr(dependency_utils, "_is_coroutine_callable", None)
        if detector is not None:
            return bool(detector(dependant.call))
        return bool(cast(Any, dependant).is_coroutine_callable)

    @staticmethod
    def _generator_context(
        dependant: Dependant,
        *,
        values: dict[str, Any],
    ) -> AbstractAsyncContextManager[Any]:
        call = cast(Callable[..., Any], dependant.call)
        if DependencyInstrumentation._is_async_generator(dependant):
            return cast(
                AbstractAsyncContextManager[Any],
                _utils.asynccontextmanager(call)(**values),
            )
        context_manager = _utils.contextmanager(call)(**values)
        return cast(
            AbstractAsyncContextManager[Any],
            _utils.contextmanager_in_threadpool(context_manager),
        )

    @staticmethod
    def _cache_key(
        dependant: Dependant,
        *,
        uses_scopes_cache: Any,
    ) -> DependencyCacheKey:
        key_factory = getattr(dependency_utils, "_get_cache_key", None)
        if key_factory is not None:
            return cast(
                DependencyCacheKey,
                key_factory(
                    dependant=dependant,
                    uses_scopes_cache=uses_scopes_cache,
                ),
            )
        return cast(DependencyCacheKey, cast(Any, dependant).cache_key)

    @staticmethod
    def _oauth_scopes(dependant: Dependant) -> list[str]:
        getter = getattr(dependency_utils, "_get_oauth_scopes", None)
        if getter is not None:
            return cast(list[str], getter(dependant=dependant))
        legacy_dependant = cast(Any, dependant)
        scopes = getattr(legacy_dependant, "oauth_scopes", None)
        if scopes is None:
            scopes = getattr(legacy_dependant, "security_scopes", None)
        return cast(list[str], scopes or [])

    def _override_dependant(
        self,
        dependant: Dependant,
        *,
        call: Callable[..., Any],
    ) -> Dependant:
        parameters = inspect.signature(dependency_utils.get_dependant).parameters
        kwargs: dict[str, Any] = {
            "path": dependant.path,
            "call": call,
            "name": dependant.name,
            "scope": dependant.scope,
        }
        if "parent_oauth_scopes" in parameters:
            kwargs["parent_oauth_scopes"] = self._oauth_scopes(dependant)
        else:
            kwargs["security_scopes"] = self._oauth_scopes(dependant)
        return dependency_utils.get_dependant(**kwargs)


dependency_instrumentation = DependencyInstrumentation()

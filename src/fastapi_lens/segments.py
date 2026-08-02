"""Public custom segment context managers and decorators."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from time import perf_counter_ns
from typing import Any, ParamSpec, TypeVar, cast
from uuid import uuid4

from fastapi_lens.collector import TraceCollector
from fastapi_lens.context import (
    current_collector,
    enter_segment,
    exit_segment,
)
from fastapi_lens.models import (
    JsonValue,
    SegmentStatus,
    SegmentType,
    TraceError,
    TraceSegment,
)

P = ParamSpec("P")
R = TypeVar("R")


class _SegmentScope:
    def __init__(
        self,
        name: str,
        attributes: Mapping[str, JsonValue] | None,
    ) -> None:
        self._name = name
        self._attributes = attributes
        self._collector: TraceCollector | None = None
        self._segment: TraceSegment | None = None
        self._stack_token: Any = None

    def start(self) -> TraceSegment | None:
        collector = current_collector()
        if collector is None:
            return None
        segment = TraceSegment(
            id=uuid4().hex,
            trace_id=collector.trace.id,
            type=SegmentType.CUSTOM,
            name=self._name,
            start_ns=perf_counter_ns(),
            attributes=dict(self._attributes) if self._attributes is not None else {},
        )
        stack_token = enter_segment(segment)
        if stack_token is None:
            return None
        self._collector = collector
        self._segment = segment
        self._stack_token = stack_token
        return segment

    def finish(self, error: BaseException | None) -> None:
        collector = self._collector
        segment = self._segment
        if collector is None or segment is None:
            return
        if error is None:
            status = SegmentStatus.OK
            trace_error = None
        elif isinstance(error, asyncio.CancelledError):
            status = SegmentStatus.CANCELLED
            trace_error = TraceError(
                type=type(error).__name__,
                message=str(error),
            )
        else:
            status = SegmentStatus.ERROR
            trace_error = TraceError(
                type=type(error).__name__,
                message=str(error),
            )
        collector.finish_segment(
            segment,
            end_ns=perf_counter_ns(),
            status=status,
            error=trace_error,
        )
        exit_segment(self._stack_token)


class _SyncSegmentContext:
    def __init__(
        self,
        name: str,
        attributes: Mapping[str, JsonValue] | None,
    ) -> None:
        self._scope = _SegmentScope(name, attributes)

    def __enter__(self) -> TraceSegment | None:
        return self._scope.start()

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: Any,
    ) -> None:
        self._scope.finish(error)


class _AsyncSegmentContext:
    def __init__(
        self,
        name: str,
        attributes: Mapping[str, JsonValue] | None,
    ) -> None:
        self._scope = _SegmentScope(name, attributes)

    async def __aenter__(self) -> TraceSegment | None:
        return self._scope.start()

    async def __aexit__(
        self,
        _error_type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: Any,
    ) -> None:
        self._scope.finish(error)


class CurrentTrace:
    """Create custom segments against the active request trace."""

    def segment(
        self,
        name: str,
        *,
        attributes: Mapping[str, JsonValue] | None = None,
    ) -> _AsyncSegmentContext:
        """Return an asynchronous custom segment context manager."""
        return _AsyncSegmentContext(name, attributes)

    def segment_sync(
        self,
        name: str,
        *,
        attributes: Mapping[str, JsonValue] | None = None,
    ) -> _SyncSegmentContext:
        """Return a synchronous custom segment context manager."""
        return _SyncSegmentContext(name, attributes)


current_trace = CurrentTrace()


def trace_segment(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a synchronous or asynchronous callable with a custom segment."""

    def decorator(function: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(function):
            async_function = cast(Callable[P, Awaitable[Any]], function)

            @wraps(function)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                async with current_trace.segment(name):
                    return await async_function(*args, **kwargs)

            return cast(Callable[P, R], async_wrapper)

        @wraps(function)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with current_trace.segment_sync(name):
                return function(*args, **kwargs)

        return sync_wrapper

    return decorator

"""Reference-counted FastAPI endpoint handler instrumentation."""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from threading import RLock
from time import perf_counter_ns
from typing import Any, cast
from uuid import uuid4

import fastapi.routing

from fastapi_latensight.context import current_collector, enter_segment, exit_segment
from fastapi_latensight.models import (
    SegmentStatus,
    SegmentType,
    TraceError,
    TraceSegment,
)

EndpointRunner = Callable[..., Awaitable[Any]]


class HandlerInstrumentation:
    """Install one process-global handler wrapper with shared ownership."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._owners: set[object] = set()
        self._original: EndpointRunner = fastapi.routing.run_endpoint_function
        self._wrapper = self._make_wrapper()

    @property
    def installed(self) -> bool:
        """Return whether the adapter wrapper is currently installed."""
        with self._lock:
            return fastapi.routing.run_endpoint_function is self._wrapper

    def install(self, owner: object) -> None:
        """Install for one owner without wrapping more than once."""
        with self._lock:
            if owner in self._owners:
                return
            if not self._owners:
                if fastapi.routing.run_endpoint_function is not self._original:
                    raise RuntimeError(
                        "FastAPI handler instrumentation target was replaced."
                    )
                fastapi.routing.run_endpoint_function = cast(Any, self._wrapper)
            self._owners.add(owner)

    def remove(self, owner: object) -> None:
        """Remove one owner and restore after the final owner leaves."""
        with self._lock:
            self._owners.discard(owner)
            if not self._owners and self.installed:
                fastapi.routing.run_endpoint_function = cast(Any, self._original)

    def restore_all(self) -> None:
        """Remove all owners and restore the exact original callable."""
        with self._lock:
            self._owners.clear()
            if self.installed:
                fastapi.routing.run_endpoint_function = cast(Any, self._original)

    def _make_wrapper(self) -> EndpointRunner:
        @wraps(self._original)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            collector = current_collector()
            if collector is None:
                return await self._original(*args, **kwargs)

            dependant = kwargs.get("dependant")
            endpoint = getattr(dependant, "call", None)
            name = getattr(endpoint, "__name__", type(endpoint).__name__)
            is_coroutine = bool(kwargs.get("is_coroutine"))
            segment = TraceSegment(
                id=uuid4().hex,
                trace_id=collector.trace.id,
                type=SegmentType.HANDLER,
                name=name,
                start_ns=perf_counter_ns(),
                attributes={
                    "execution_mode": (
                        "async_wall_clock" if is_coroutine else "thread_pool_wall_clock"
                    )
                },
            )
            stack_token = enter_segment(segment)
            if stack_token is None:
                return await self._original(*args, **kwargs)

            error: BaseException | None = None
            try:
                return await self._original(*args, **kwargs)
            except BaseException as caught:
                error = caught
                raise
            finally:
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
                exit_segment(stack_token)

        return wrapper


handler_instrumentation = HandlerInstrumentation()

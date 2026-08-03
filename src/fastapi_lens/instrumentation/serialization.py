"""Reference-counted FastAPI response serialization instrumentation."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import RLock
from time import perf_counter_ns
from typing import Any
from uuid import uuid4

import fastapi
import fastapi.routing

from fastapi_lens.context import current_collector, enter_segment, exit_segment
from fastapi_lens.models import (
    SegmentStatus,
    SegmentType,
    TraceError,
    TraceSegment,
)

SerializationHook = Callable[..., Awaitable[Any]]
_routing: Any = fastapi.routing

_BASE_PARAMETERS = {
    "field",
    "response_content",
    "include",
    "exclude",
    "by_alias",
    "exclude_unset",
    "exclude_defaults",
    "exclude_none",
    "is_coroutine",
}
_OPTIONAL_PARAMETERS = {"endpoint_ctx", "dump_json"}


@dataclass(slots=True, frozen=True)
class SerializationCapabilities:
    """Optional FastAPI serialization parameters supported by this adapter."""

    endpoint_context: bool
    dump_json: bool


class SerializationInstrumentation:
    """Install one process-global serialization wrapper with shared ownership."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._owners: set[object] = set()
        self._original: SerializationHook = _routing.serialize_response
        parameters = inspect.signature(self._original).parameters
        self.capabilities = SerializationCapabilities(
            endpoint_context="endpoint_ctx" in parameters,
            dump_json="dump_json" in parameters,
        )
        self._wrapper = self._make_wrapper()

    @property
    def installed(self) -> bool:
        """Return whether the serialization adapter is installed."""
        with self._lock:
            return _routing.serialize_response is self._wrapper

    def install(self, owner: object) -> None:
        """Install for one owner after validating the FastAPI boundary."""
        with self._lock:
            if owner in self._owners:
                return
            if not self._owners:
                self._validate_compatibility()
                if _routing.serialize_response is not self._original:
                    raise RuntimeError(
                        "FastAPI serialization instrumentation target was replaced."
                    )
                _routing.serialize_response = self._wrapper
            self._owners.add(owner)

    def remove(self, owner: object) -> None:
        """Remove one owner and restore after the final owner leaves."""
        with self._lock:
            self._owners.discard(owner)
            if not self._owners and self.installed:
                _routing.serialize_response = self._original

    def restore_all(self) -> None:
        """Remove every owner and restore the exact original callable."""
        with self._lock:
            self._owners.clear()
            if self.installed:
                _routing.serialize_response = self._original

    def _validate_compatibility(self) -> None:
        version = tuple(int(part) for part in fastapi.__version__.split(".")[:3])
        if not (version >= (0, 121, 0) and version < (0, 142, 0)):
            raise RuntimeError(
                f"FastAPI {fastapi.__version__} has no serialization adapter."
            )
        parameters = set(inspect.signature(self._original).parameters)
        if (
            not _BASE_PARAMETERS.issubset(parameters)
            or parameters - _BASE_PARAMETERS - _OPTIONAL_PARAMETERS
        ):
            raise RuntimeError(
                "FastAPI serialize_response has an unsupported signature."
            )
        try:
            source = inspect.getsource(self._original)
        except (OSError, TypeError) as error:
            raise RuntimeError(
                "FastAPI serialize_response source cannot be verified."
            ) from error
        required_markers = ("response_content", "jsonable_encoder", "field")
        if not all(marker in source for marker in required_markers):
            raise RuntimeError(
                "FastAPI serialize_response behavior guard did not match."
            )

    def _make_wrapper(self) -> SerializationHook:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            collector = current_collector()
            if collector is None:
                return await self._original(*args, **kwargs)

            has_response_model = kwargs.get("field") is not None
            is_coroutine = bool(kwargs.get("is_coroutine", True))
            segment = TraceSegment(
                id=uuid4().hex,
                trace_id=collector.trace.id,
                type=SegmentType.SERIALIZATION,
                name="response serialization",
                start_ns=perf_counter_ns(),
                attributes={
                    "has_response_model": has_response_model,
                    "handler_mode": "async" if is_coroutine else "sync",
                },
            )
            token = enter_segment(segment)
            if token is None:
                return await self._original(*args, **kwargs)

            caught: BaseException | None = None
            try:
                return await self._original(*args, **kwargs)
            except BaseException as error:
                caught = error
                raise
            finally:
                if caught is None:
                    status = SegmentStatus.OK
                    trace_error = None
                elif isinstance(caught, asyncio.CancelledError):
                    status = SegmentStatus.CANCELLED
                    trace_error = TraceError(
                        type=type(caught).__name__,
                        message=str(caught),
                    )
                else:
                    status = SegmentStatus.ERROR
                    trace_error = TraceError(
                        type=type(caught).__name__,
                        message=str(caught),
                    )
                collector.finish_segment(
                    segment,
                    end_ns=perf_counter_ns(),
                    status=status,
                    error=trace_error,
                )
                exit_segment(token)

        return wrapper


serialization_instrumentation = SerializationInstrumentation()

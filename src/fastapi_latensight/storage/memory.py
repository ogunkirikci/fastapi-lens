"""Thread-safe bounded process-local trace storage."""

from threading import RLock
from typing import ClassVar

from fastapi_latensight.exporters.json import (
    DEFAULT_MAX_TRACE_BYTES,
    trace_snapshot_to_json,
)
from fastapi_latensight.models import RequestTraceSnapshot


class MemoryTraceStore:
    """Bounded in-memory storage isolated to the current Python process."""

    process_local: ClassVar[bool] = True

    def __init__(
        self,
        *,
        max_traces: int = 500,
        max_page_size: int = 200,
        max_trace_bytes: int = DEFAULT_MAX_TRACE_BYTES,
    ) -> None:
        if max_traces <= 0:
            raise ValueError("max_traces must be greater than zero.")
        if max_page_size <= 0:
            raise ValueError("max_page_size must be greater than zero.")
        if max_trace_bytes <= 0:
            raise ValueError("max_trace_bytes must be greater than zero.")
        self._max_traces = max_traces
        self._max_page_size = max_page_size
        self._max_trace_bytes = max_trace_bytes
        self._traces: dict[str, RequestTraceSnapshot] = {}
        self._lock = RLock()

    async def save(self, trace: RequestTraceSnapshot) -> None:
        """Save a snapshot and evict the oldest trace when over capacity."""
        if not isinstance(trace, RequestTraceSnapshot):
            raise TypeError("MemoryTraceStore accepts RequestTraceSnapshot only.")
        trace_snapshot_to_json(trace, max_bytes=self._max_trace_bytes)
        with self._lock:
            self._traces[trace.id] = trace
            while len(self._traces) > self._max_traces:
                oldest = min(self._traces.values(), key=self._ordering_key)
                del self._traces[oldest.id]

    async def get(self, trace_id: str) -> RequestTraceSnapshot | None:
        """Return a snapshot by ID when present."""
        with self._lock:
            return self._traces.get(trace_id)

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RequestTraceSnapshot]:
        """Return newest snapshots first using stable pagination."""
        self._validate_pagination(limit=limit, offset=offset)
        with self._lock:
            ordered = sorted(
                self._traces.values(),
                key=self._ordering_key,
                reverse=True,
            )
            return ordered[offset : offset + limit]

    async def clear(self) -> None:
        """Remove every snapshot from this process-local store."""
        with self._lock:
            self._traces.clear()

    @staticmethod
    def _ordering_key(trace: RequestTraceSnapshot) -> tuple[int, str]:
        completed_ns = trace.application_completed_ns
        return (-1 if completed_ns is None else completed_ns, trace.id)

    def _validate_pagination(self, *, limit: int, offset: int) -> None:
        if limit <= 0:
            raise ValueError("limit must be greater than zero.")
        if limit > self._max_page_size:
            raise ValueError(
                f"limit must not exceed max_page_size ({self._max_page_size})."
            )
        if offset < 0:
            raise ValueError("offset must be zero or greater.")

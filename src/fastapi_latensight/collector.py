"""Thread-safe mutable trace collection and finalization."""

from threading import RLock

from fastapi_latensight.models import (
    LogicalDependencyNode,
    RequestTrace,
    RequestTraceSnapshot,
    SegmentStatus,
    TraceError,
    TraceSegment,
)


class TraceCollector:
    """Own mutable request state until it is finalized into one snapshot."""

    def __init__(self, trace: RequestTrace) -> None:
        self._trace = trace
        self._lock = RLock()
        self._snapshot: RequestTraceSnapshot | None = None
        self._late_segment_count = 0

    @property
    def trace(self) -> RequestTrace:
        """Return the mutable trace owned by this collector."""
        return self._trace

    @property
    def finalized(self) -> bool:
        """Return whether this collector has produced its immutable snapshot."""
        with self._lock:
            return self._snapshot is not None

    @property
    def late_segment_count(self) -> int:
        """Return the number of segment writes rejected after finalization."""
        with self._lock:
            return self._late_segment_count

    def add_segment(self, segment: TraceSegment) -> bool:
        """Add a segment atomically, or reject it after finalization."""
        with self._lock:
            if self._snapshot is not None:
                self._late_segment_count += 1
                return False
            self._trace.segments.append(segment)
            return True

    def add_logical_dependency(self, dependency: LogicalDependencyNode) -> bool:
        """Add a logical dependency atomically, or reject it after finalization."""
        with self._lock:
            if self._snapshot is not None:
                self._late_segment_count += 1
                return False
            self._trace.logical_dependencies.append(dependency)
            return True

    def finish_segment(
        self,
        segment: TraceSegment,
        *,
        end_ns: int,
        status: SegmentStatus,
        error: TraceError | None,
    ) -> bool:
        """Finish a segment atomically, or reject the late write."""
        with self._lock:
            if self._snapshot is not None:
                self._late_segment_count += 1
                return False
            segment.end_ns = end_ns
            segment.status = status
            segment.error = error
            return True

    def finalize(self) -> RequestTraceSnapshot:
        """Return one stable immutable snapshot for this collector."""
        with self._lock:
            if self._snapshot is None:
                self._snapshot = self._trace.snapshot()
            return self._snapshot

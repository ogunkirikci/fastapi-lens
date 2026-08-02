"""Storage protocol for finalized immutable trace snapshots."""

from typing import Protocol

from fastapi_lens.models import RequestTraceSnapshot


class TraceStore(Protocol):
    """Asynchronous storage contract used by request finalization."""

    async def save(self, trace: RequestTraceSnapshot) -> None:
        """Persist one finalized immutable snapshot."""
        ...

    async def get(self, trace_id: str) -> RequestTraceSnapshot | None:
        """Return one snapshot by ID when present."""
        ...

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RequestTraceSnapshot]:
        """Return a stable page of snapshots."""
        ...

    async def clear(self) -> None:
        """Remove every snapshot from this store."""
        ...

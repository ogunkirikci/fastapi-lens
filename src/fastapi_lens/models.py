"""Core trace domain models."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

JsonValue: TypeAlias = (
    bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
)
FrozenJsonValue: TypeAlias = (
    bool
    | int
    | float
    | str
    | tuple["FrozenJsonValue", ...]
    | tuple[tuple[str, "FrozenJsonValue"], ...]
    | None
)


class SegmentType(StrEnum):
    """A measured phase or operation within a request trace."""

    DEPENDENCY_SETUP = "dependency_setup"
    DEPENDENCY_CLEANUP = "dependency_cleanup"
    HANDLER = "handler"
    SQL = "sql"
    SERIALIZATION = "serialization"
    RESPONSE_SEND = "response_send"
    CUSTOM = "custom"


class SegmentStatus(StrEnum):
    """The terminal or current state of a trace segment."""

    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"


@dataclass(slots=True, frozen=True)
class TraceError:
    """Redacted structured error information captured during tracing."""

    type: str
    message: str
    stack: str | None = None


@dataclass(slots=True, frozen=True)
class Diagnostic:
    """A diagnostic finding associated with a trace or segment."""

    code: str
    severity: str
    message: str
    segment_id: str | None = None


@dataclass(slots=True)
class TraceSegment:
    """A mutable measured operation owned by an in-flight request trace."""

    id: str
    trace_id: str
    type: SegmentType
    name: str
    start_ns: int
    end_ns: int | None = None
    parent_id: str | None = None
    logical_dependency_id: str | None = None
    status: SegmentStatus = SegmentStatus.INCOMPLETE
    attributes: dict[str, JsonValue] = field(default_factory=dict)
    error: TraceError | None = None

    @property
    def duration_ms(self) -> float | None:
        """Return the measured wall-clock duration without clamping."""
        if self.end_ns is None:
            return None
        return (self.end_ns - self.start_ns) / 1_000_000

"""Core trace domain models."""

from dataclasses import dataclass
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

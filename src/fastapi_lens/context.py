"""Request-local collector and immutable execution stack management."""

from contextvars import ContextVar, Token
from dataclasses import dataclass

from fastapi_lens.collector import TraceCollector
from fastapi_lens.models import TraceSegment

_current_collector: ContextVar[TraceCollector | None] = ContextVar(
    "fastapi_lens_current_collector",
    default=None,
)
_segment_stack: ContextVar[tuple[str, ...]] = ContextVar(
    "fastapi_lens_segment_stack",
    default=(),
)


@dataclass(slots=True, frozen=True)
class RequestContextToken:
    """Tokens required to restore a previously active request context."""

    collector: Token[TraceCollector | None]
    segment_stack: Token[tuple[str, ...]]


def bind_request_context(collector: TraceCollector) -> RequestContextToken:
    """Bind a collector with a fresh segment stack to the current context."""
    collector_token = _current_collector.set(collector)
    stack_token = _segment_stack.set(())
    return RequestContextToken(
        collector=collector_token,
        segment_stack=stack_token,
    )


def reset_request_context(token: RequestContextToken) -> None:
    """Restore the collector and stack that preceded a context binding."""
    _segment_stack.reset(token.segment_stack)
    _current_collector.reset(token.collector)


def current_collector() -> TraceCollector | None:
    """Return the active collector, if any."""
    return _current_collector.get()


def current_segment_stack() -> tuple[str, ...]:
    """Return the immutable execution stack for the current context."""
    return _segment_stack.get()


def current_parent_segment_id() -> str | None:
    """Return the current execution parent without changing the stack."""
    stack = _segment_stack.get()
    return stack[-1] if stack else None


def enter_segment(segment: TraceSegment) -> Token[tuple[str, ...]] | None:
    """Record and push a segment, or act as a no-op without an active trace."""
    collector = _current_collector.get()
    if collector is None:
        return None

    stack = _segment_stack.get()
    if segment.parent_id is None and stack:
        segment.parent_id = stack[-1]
    if not collector.add_segment(segment):
        return None
    return _segment_stack.set((*stack, segment.id))


def exit_segment(token: Token[tuple[str, ...]] | None) -> None:
    """Restore a previous segment stack; no-op tokens require no work."""
    if token is not None:
        _segment_stack.reset(token)

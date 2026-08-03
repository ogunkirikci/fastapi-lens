"""Explicit SQLAlchemy engine instrumentation."""

import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass, field
from threading import RLock
from time import perf_counter_ns
from typing import Any
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from fastapi_lens.collector import TraceCollector
from fastapi_lens.context import current_collector, enter_segment, exit_segment
from fastapi_lens.models import (
    JsonValue,
    SegmentStatus,
    SegmentType,
    TraceError,
    TraceSegment,
)

SQLALCHEMY_INTERNAL_EXECUTION_OPTION = "fastapi_lens_internal"
DEFAULT_MAX_SQL_LENGTH = 2_000

_COMMENT_PATTERN = re.compile(r"--[^\r\n]*|/\*.*?\*/", re.DOTALL)
_STRING_LITERAL_PATTERN = re.compile(r"'(?:''|[^'])*'")
_NUMERIC_LITERAL_PATTERN = re.compile(
    r"(?<![\w:$])[-+]?(?:0x[0-9a-f]+|\d+(?:\.\d+)?(?:e[-+]?\d+)?)(?![\w$])",
    re.IGNORECASE,
)
_KEYWORD_LITERAL_PATTERN = re.compile(r"\b(?:true|false|null)\b", re.IGNORECASE)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_OPERATION_PATTERN = re.compile(r"^[\s(]*([A-Za-z]+)")


def normalize_sql(statement: str) -> str:
    """Return whitespace-normalized SQL with common literal forms masked."""
    without_comments = _COMMENT_PATTERN.sub(" ", statement)
    without_strings = _STRING_LITERAL_PATTERN.sub("?", without_comments)
    without_numbers = _NUMERIC_LITERAL_PATTERN.sub("?", without_strings)
    without_keywords = _KEYWORD_LITERAL_PATTERN.sub("?", without_numbers)
    return _WHITESPACE_PATTERN.sub(" ", without_keywords).strip()


def fingerprint_sql(normalized_statement: str) -> str:
    """Return a stable SHA-256 fingerprint for full normalized SQL."""
    return hashlib.sha256(normalized_statement.encode("utf-8")).hexdigest()


def sql_operation(normalized_statement: str) -> str:
    """Return the first SQL keyword, or UNKNOWN when none is available."""
    match = _OPERATION_PATTERN.match(normalized_statement)
    return match.group(1).upper() if match is not None else "UNKNOWN"


def truncate_sql(statement: str, *, max_length: int) -> str:
    """Truncate display SQL without changing the fingerprint input."""
    if len(statement) <= max_length:
        return statement
    return f"{statement[: max_length - 1]}…"


@dataclass(slots=True)
class _SqlExecution:
    collector: TraceCollector
    segment: TraceSegment
    stack_token: Any
    context_id: int


@dataclass(slots=True)
class _EngineRegistration:
    engine: Engine
    owners: set[object] = field(default_factory=set)


class SqlAlchemyInstrumentation:
    """Manage listeners for explicitly registered SQLAlchemy engines."""

    def __init__(self, *, max_sql_length: int = DEFAULT_MAX_SQL_LENGTH) -> None:
        if max_sql_length <= 0:
            raise ValueError("max_sql_length must be greater than zero.")
        self._max_sql_length = max_sql_length
        self._lock = RLock()
        self._registrations: dict[int, _EngineRegistration] = {}
        self._stack_key = f"fastapi_lens_sql_stack_{id(self)}"
        self._before_listener = self._before_cursor_execute
        self._after_listener = self._after_cursor_execute
        self._error_listener = self._handle_error

    @property
    def registered_engine_count(self) -> int:
        """Return the number of event targets with active owners."""
        with self._lock:
            return len(self._registrations)

    def is_registered(self, engine: Engine | AsyncEngine) -> bool:
        """Return whether the engine's synchronous target is registered."""
        target = self._target(engine)
        with self._lock:
            return id(target) in self._registrations

    def register(self, engine: Engine | AsyncEngine, owner: object) -> None:
        """Register one owner without adding duplicate event listeners."""
        target = self._target(engine)
        target_id = id(target)
        with self._lock:
            registration = self._registrations.get(target_id)
            if registration is not None:
                registration.owners.add(owner)
                return
            event.listen(target, "before_cursor_execute", self._before_listener)
            event.listen(target, "after_cursor_execute", self._after_listener)
            event.listen(target, "handle_error", self._error_listener)
            self._registrations[target_id] = _EngineRegistration(
                engine=target,
                owners={owner},
            )

    def unregister(self, engine: Engine | AsyncEngine, owner: object) -> None:
        """Remove one owner and detach listeners after the final owner."""
        target = self._target(engine)
        target_id = id(target)
        with self._lock:
            registration = self._registrations.get(target_id)
            if registration is None:
                return
            registration.owners.discard(owner)
            if registration.owners:
                return
            self._remove_registration(target_id, registration)

    def unregister_owner(self, owner: object) -> None:
        """Remove an owner from every engine it registered."""
        with self._lock:
            for target_id, registration in tuple(self._registrations.items()):
                registration.owners.discard(owner)
                if not registration.owners:
                    self._remove_registration(target_id, registration)

    def restore_all(self) -> None:
        """Detach every listener installed by this registry."""
        with self._lock:
            for target_id, registration in tuple(self._registrations.items()):
                self._remove_registration(target_id, registration)

    def _remove_registration(
        self,
        target_id: int,
        registration: _EngineRegistration,
    ) -> None:
        event.remove(
            registration.engine,
            "before_cursor_execute",
            self._before_listener,
        )
        event.remove(
            registration.engine,
            "after_cursor_execute",
            self._after_listener,
        )
        event.remove(registration.engine, "handle_error", self._error_listener)
        del self._registrations[target_id]

    @staticmethod
    def _target(engine: Engine | AsyncEngine) -> Engine:
        if isinstance(engine, AsyncEngine):
            return engine.sync_engine
        if isinstance(engine, Engine):
            return engine
        raise TypeError("Expected a SQLAlchemy Engine or AsyncEngine.")

    def _before_cursor_execute(
        self,
        connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        try:
            collector = current_collector()
            if collector is None or collector.finalized or self._is_internal(context):
                return
            normalized = normalize_sql(statement)
            operation = sql_operation(normalized)
            attributes: dict[str, JsonValue] = {
                "dialect": connection.dialect.name,
                "executemany": executemany,
                "operation": operation,
                "statement": truncate_sql(
                    normalized,
                    max_length=self._max_sql_length,
                ),
                "statement_fingerprint": fingerprint_sql(normalized),
            }
            segment = TraceSegment(
                id=uuid4().hex,
                trace_id=collector.trace.id,
                type=SegmentType.SQL,
                name=f"{operation} query",
                start_ns=perf_counter_ns(),
                attributes=attributes,
            )
            stack_token = enter_segment(segment)
            if stack_token is None:
                return
            self._stack(connection.info).append(
                _SqlExecution(
                    collector=collector,
                    segment=segment,
                    stack_token=stack_token,
                    context_id=id(context),
                )
            )
        except Exception:
            return

    def _after_cursor_execute(
        self,
        connection: Connection,
        cursor: Any,
        _statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        try:
            execution = self._pop_execution(
                connection.info,
                context=_context,
            )
            if execution is None:
                return
            row_count = getattr(cursor, "rowcount", None)
            if isinstance(row_count, int) and row_count >= 0:
                execution.segment.attributes["row_count"] = row_count
            execution.collector.finish_segment(
                execution.segment,
                end_ns=perf_counter_ns(),
                status=SegmentStatus.OK,
                error=None,
            )
            exit_segment(execution.stack_token)
        except Exception:
            return

    def _handle_error(self, context: Any) -> None:
        try:
            if getattr(context, "is_pre_ping", False):
                return
            connection = getattr(context, "connection", None)
            if connection is None:
                return
            execution_context = getattr(context, "execution_context", None)
            if execution_context is None:
                return
            execution = self._pop_execution(
                connection.info,
                context=execution_context,
            )
            if execution is None:
                return
            original_error = getattr(context, "original_exception", None)
            error_type = (
                type(original_error).__name__
                if original_error is not None
                else "DatabaseError"
            )
            execution.collector.finish_segment(
                execution.segment,
                end_ns=perf_counter_ns(),
                status=SegmentStatus.ERROR,
                error=TraceError(
                    type=error_type,
                    message="Database execution failed.",
                ),
            )
            exit_segment(execution.stack_token)
        except Exception:
            return

    @staticmethod
    def _is_internal(context: Any) -> bool:
        execution_options = getattr(context, "execution_options", {})
        return bool(execution_options.get(SQLALCHEMY_INTERNAL_EXECUTION_OPTION, False))

    def _stack(self, info: dict[str, Any]) -> list[_SqlExecution]:
        stack = info.get(self._stack_key)
        if isinstance(stack, list):
            return stack
        created: list[_SqlExecution] = []
        info[self._stack_key] = created
        return created

    def _pop_execution(
        self,
        info: dict[str, Any],
        *,
        context: Any | None = None,
    ) -> _SqlExecution | None:
        stack = self._stack(info)
        if not stack:
            return None
        if context is not None and stack[-1].context_id != id(context):
            return None
        execution = stack.pop()
        if not stack:
            with suppress(KeyError):
                del info[self._stack_key]
        return execution


sqlalchemy_instrumentation = SqlAlchemyInstrumentation()

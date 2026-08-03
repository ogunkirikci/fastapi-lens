"""Pre-storage trace redaction and size enforcement."""

import math
from dataclasses import dataclass

from fastapi_latensight.exporters.json import trace_snapshot_to_json
from fastapi_latensight.models import (
    Diagnostic,
    JsonValue,
    RequestTrace,
    TraceError,
)

REDACTED_VALUE = "[REDACTED]"
DEFAULT_SENSITIVE_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "cookie",
        "passwd",
        "password",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "set_cookie",
        "token",
        "x_api_key",
    }
)


def truncate_text(value: str, *, max_length: int) -> str:
    """Return text bounded to max_length Unicode code points."""
    if len(value) <= max_length:
        return value
    if max_length == 1:
        return "…"
    return f"{value[: max_length - 1]}…"


def _normalized_field_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")


@dataclass(slots=True, frozen=True)
class TraceSanitizerConfig:
    """Limits applied before a trace can reach storage."""

    max_segments_per_trace: int = 1_000
    max_dependencies_per_trace: int = 1_000
    max_diagnostics_per_trace: int = 200
    max_attributes_per_segment: int = 100
    max_collection_items: int = 100
    max_nesting_depth: int = 10
    max_attribute_length: int = 2_000
    max_error_length: int = 8_000
    max_trace_bytes: int = 1_000_000
    sensitive_fields: frozenset[str] = DEFAULT_SENSITIVE_FIELDS

    def __post_init__(self) -> None:
        numeric_limits = (
            self.max_segments_per_trace,
            self.max_dependencies_per_trace,
            self.max_diagnostics_per_trace,
            self.max_attributes_per_segment,
            self.max_collection_items,
            self.max_nesting_depth,
            self.max_attribute_length,
            self.max_error_length,
            self.max_trace_bytes,
        )
        if any(limit <= 0 for limit in numeric_limits):
            raise ValueError("Trace sanitizer limits must be greater than zero.")


@dataclass(slots=True)
class _SanitizationState:
    truncated: bool = False


class TraceSanitizer:
    """Redact and bound mutable trace data before snapshot storage."""

    def __init__(self, config: TraceSanitizerConfig | None = None) -> None:
        self.config = config if config is not None else TraceSanitizerConfig()
        self._sensitive_fields = frozenset(
            _normalized_field_name(field) for field in self.config.sensitive_fields
        )

    def sanitize(self, trace: RequestTrace) -> None:
        """Mutate one collector-owned trace into its storage-safe form."""
        state = _SanitizationState()
        trace.method = self._field_text(trace.method, state=state)
        trace.path = self._field_text(trace.path, state=state)
        if trace.route is not None:
            trace.route = self._field_text(trace.route, state=state)
        trace.error = self._error(trace.error, state=state)

        if len(trace.segments) > self.config.max_segments_per_trace:
            del trace.segments[self.config.max_segments_per_trace :]
            state.truncated = True
        if len(trace.logical_dependencies) > self.config.max_dependencies_per_trace:
            del trace.logical_dependencies[self.config.max_dependencies_per_trace :]
            state.truncated = True
        if len(trace.diagnostics) > self.config.max_diagnostics_per_trace:
            del trace.diagnostics[self.config.max_diagnostics_per_trace :]
            state.truncated = True

        for segment in trace.segments:
            segment.name = self._field_text(segment.name, state=state)
            segment.error = self._error(segment.error, state=state)
            segment.attributes = self._mapping(
                segment.attributes,
                depth=0,
                max_items=self.config.max_attributes_per_segment,
                state=state,
            )
        for dependency in trace.logical_dependencies:
            dependency.name = self._field_text(dependency.name, state=state)
        trace.diagnostics[:] = [
            Diagnostic(
                code=self._field_text(diagnostic.code, state=state),
                severity=self._field_text(diagnostic.severity, state=state),
                message=self._truncate(
                    diagnostic.message,
                    max_length=self.config.max_error_length,
                    state=state,
                ),
                segment_id=diagnostic.segment_id,
            )
            for diagnostic in trace.diagnostics
        ]

        if state.truncated:
            self._add_truncation_diagnostic(trace)
        if self._trace_size(trace) > self.config.max_trace_bytes:
            self._shrink_to_byte_limit(trace)

    def _mapping(
        self,
        value: dict[str, JsonValue],
        *,
        depth: int,
        max_items: int,
        state: _SanitizationState,
    ) -> dict[str, JsonValue]:
        sanitized: dict[str, JsonValue] = {}
        if len(value) > max_items:
            state.truncated = True
        for key in sorted(value)[:max_items]:
            safe_key = self._field_text(key, state=state)
            if _normalized_field_name(key) in self._sensitive_fields:
                sanitized[safe_key] = REDACTED_VALUE
            else:
                sanitized[safe_key] = self._value(
                    value[key],
                    depth=depth + 1,
                    state=state,
                )
        return sanitized

    def _value(
        self,
        value: JsonValue,
        *,
        depth: int,
        state: _SanitizationState,
    ) -> JsonValue:
        if depth > self.config.max_nesting_depth:
            state.truncated = True
            return "[MAX_DEPTH]"
        if isinstance(value, str):
            return self._field_text(value, state=state)
        if isinstance(value, float) and not math.isfinite(value):
            state.truncated = True
            return None
        if isinstance(value, list):
            if len(value) > self.config.max_collection_items:
                state.truncated = True
            return [
                self._value(item, depth=depth + 1, state=state)
                for item in value[: self.config.max_collection_items]
            ]
        if isinstance(value, dict):
            return self._mapping(
                value,
                depth=depth,
                max_items=self.config.max_collection_items,
                state=state,
            )
        return value

    def _error(
        self,
        error: TraceError | None,
        *,
        state: _SanitizationState,
    ) -> TraceError | None:
        if error is None:
            return None
        return TraceError(
            type=self._field_text(error.type, state=state),
            message=self._truncate(
                error.message,
                max_length=self.config.max_error_length,
                state=state,
            ),
            stack=(
                None
                if error.stack is None
                else self._truncate(
                    error.stack,
                    max_length=self.config.max_error_length,
                    state=state,
                )
            ),
        )

    def _field_text(self, value: str, *, state: _SanitizationState) -> str:
        return self._truncate(
            value,
            max_length=self.config.max_attribute_length,
            state=state,
        )

    @staticmethod
    def _truncate(
        value: str,
        *,
        max_length: int,
        state: _SanitizationState,
    ) -> str:
        if len(value) > max_length:
            state.truncated = True
        return truncate_text(value, max_length=max_length)

    @staticmethod
    def _trace_size(trace: RequestTrace) -> int:
        return len(
            trace_snapshot_to_json(
                trace.snapshot(),
                max_bytes=2**63 - 1,
            )
        )

    def _shrink_to_byte_limit(self, trace: RequestTrace) -> None:
        self._add_truncation_diagnostic(trace)
        collections = (
            trace.segments,
            trace.logical_dependencies,
            trace.diagnostics,
        )
        while self._trace_size(trace) > self.config.max_trace_bytes:
            changed = False
            for collection in collections:
                minimum = 1 if collection is trace.diagnostics else 0
                if len(collection) <= minimum:
                    continue
                keep = max(minimum, len(collection) // 2)
                del collection[keep:]
                changed = True
                if self._trace_size(trace) <= self.config.max_trace_bytes:
                    return
            if changed:
                continue
            if trace.error is not None:
                trace.error = None
                continue
            for field_name in ("path", "route", "method"):
                value = getattr(trace, field_name)
                if isinstance(value, str) and len(value) > 1:
                    setattr(
                        trace,
                        field_name,
                        truncate_text(value, max_length=max(1, len(value) // 2)),
                    )
                    changed = True
            if not changed:
                return

    def _add_truncation_diagnostic(self, trace: RequestTrace) -> None:
        trace.diagnostics[:] = [
            diagnostic
            for diagnostic in trace.diagnostics
            if diagnostic.code != "trace_truncated"
        ]
        trace.diagnostics.insert(
            0,
            Diagnostic(
                code="trace_truncated",
                severity="warning",
                message="Trace data was truncated by pre-storage safety limits.",
            ),
        )
        del trace.diagnostics[self.config.max_diagnostics_per_trace :]

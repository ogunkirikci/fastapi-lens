import json
from dataclasses import FrozenInstanceError

import pytest

from fastapi_latensight.models import Diagnostic, SegmentStatus, SegmentType, TraceError


def test_segment_type_values_are_stable_strings() -> None:
    assert {segment_type.name: segment_type.value for segment_type in SegmentType} == {
        "DEPENDENCY_SETUP": "dependency_setup",
        "DEPENDENCY_CLEANUP": "dependency_cleanup",
        "HANDLER": "handler",
        "SQL": "sql",
        "SERIALIZATION": "serialization",
        "RESPONSE_SEND": "response_send",
        "CUSTOM": "custom",
    }
    assert json.dumps(SegmentType.HANDLER) == '"handler"'


def test_segment_status_values_are_stable_strings() -> None:
    assert {status.name: status.value for status in SegmentStatus} == {
        "OK": "ok",
        "ERROR": "error",
        "CANCELLED": "cancelled",
        "INCOMPLETE": "incomplete",
    }
    assert json.dumps(SegmentStatus.INCOMPLETE) == '"incomplete"'


def test_trace_error_is_immutable_and_has_an_optional_stack() -> None:
    error = TraceError(type="ValueError", message="Invalid value")

    assert error.stack is None
    with pytest.raises(FrozenInstanceError):
        error.message = "Changed"  # type: ignore[misc]


def test_diagnostic_is_immutable_and_may_reference_a_segment() -> None:
    diagnostic = Diagnostic(
        code="slow_dependency",
        severity="warning",
        message="Dependency exceeded the configured threshold.",
        segment_id="segment-1",
    )

    assert diagnostic.segment_id == "segment-1"
    with pytest.raises(FrozenInstanceError):
        diagnostic.segment_id = None  # type: ignore[misc]

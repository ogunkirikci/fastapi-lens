from datetime import UTC, datetime

import pytest

from fastapi_latensight.models import Diagnostic, RequestTrace


def make_trace(
    *,
    trace_id: str = "trace-1",
    response_started_ns: int | None = None,
    response_body_completed_ns: int | None = None,
    application_completed_ns: int | None = None,
) -> RequestTrace:
    return RequestTrace(
        schema_version="1.0",
        id=trace_id,
        method="GET",
        path="/items",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_received_ns=1_000_000,
        response_started_ns=response_started_ns,
        response_body_completed_ns=response_body_completed_ns,
        application_completed_ns=application_completed_ns,
    )


def test_new_trace_has_safe_incomplete_defaults() -> None:
    trace = make_trace()

    assert trace.route is None
    assert trace.status_code is None
    assert trace.segments == []
    assert trace.diagnostics == []
    assert trace.error is None
    assert trace.complete is False


def test_trace_collections_are_not_shared() -> None:
    first = make_trace(trace_id="trace-1")
    second = make_trace(trace_id="trace-2")
    finding = Diagnostic(
        code="incomplete_trace",
        severity="warning",
        message="A lifecycle checkpoint is missing.",
    )

    first.diagnostics.append(finding)

    assert first.diagnostics == [finding]
    assert second.diagnostics == []


def test_trace_calculates_all_lifecycle_durations() -> None:
    trace = make_trace(
        response_started_ns=2_000_000,
        response_body_completed_ns=5_000_000,
        application_completed_ns=7_000_000,
    )

    assert trace.time_to_response_start_ms == 1.0
    assert trace.response_complete_duration_ms == 4.0
    assert trace.response_send_duration_ms == 3.0
    assert trace.post_response_duration_ms == 2.0
    assert trace.application_duration_ms == 6.0


def test_trace_durations_are_none_when_required_checkpoints_are_missing() -> None:
    trace = make_trace()

    assert trace.time_to_response_start_ms is None
    assert trace.response_complete_duration_ms is None
    assert trace.response_send_duration_ms is None
    assert trace.post_response_duration_ms is None
    assert trace.application_duration_ms is None


def test_each_duration_only_depends_on_its_required_checkpoints() -> None:
    trace = make_trace(
        response_body_completed_ns=5_000_000,
        application_completed_ns=7_000_000,
    )

    assert trace.time_to_response_start_ms is None
    assert trace.response_complete_duration_ms == 4.0
    assert trace.response_send_duration_ms is None
    assert trace.post_response_duration_ms == 2.0
    assert trace.application_duration_ms == 6.0


def test_trace_durations_preserve_invalid_negative_intervals() -> None:
    trace = make_trace(
        response_started_ns=900_000,
        response_body_completed_ns=800_000,
        application_completed_ns=700_000,
    )

    assert trace.time_to_response_start_ms == pytest.approx(-0.1)
    assert trace.response_complete_duration_ms == pytest.approx(-0.2)
    assert trace.response_send_duration_ms == pytest.approx(-0.1)
    assert trace.post_response_duration_ms == pytest.approx(-0.1)
    assert trace.application_duration_ms == pytest.approx(-0.3)

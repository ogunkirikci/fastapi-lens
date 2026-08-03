from datetime import UTC, datetime

import pytest

from fastapi_latensight.diagnostics import (
    DiagnosticConfig,
    DiagnosticEngine,
    ExpensiveSerializationRule,
    IntegrityRule,
    PossibleNPlusOneRule,
    RepeatedQueryRule,
    SlowDependencyRule,
    SlowRequestRule,
)
from fastapi_latensight.models import (
    DependencyCacheStatus,
    Diagnostic,
    LogicalDependencyNode,
    RequestTrace,
    RequestTraceSnapshot,
    SegmentStatus,
    SegmentType,
    TraceSegment,
)

NS_PER_MS = 1_000_000


def make_segment(
    segment_id: str,
    *,
    segment_type: SegmentType,
    start_ms: int,
    end_ms: int | None,
    name: str = "operation",
    trace_id: str = "trace-1",
    parent_id: str | None = None,
    attributes: dict[str, object] | None = None,
    status: SegmentStatus = SegmentStatus.OK,
) -> TraceSegment:
    return TraceSegment(
        id=segment_id,
        trace_id=trace_id,
        type=segment_type,
        name=name,
        start_ns=start_ms * NS_PER_MS,
        end_ns=None if end_ms is None else end_ms * NS_PER_MS,
        parent_id=parent_id,
        status=status,
        attributes={} if attributes is None else attributes,  # type: ignore[arg-type]
    )


def make_trace(
    *,
    response_complete_ms: int | None = 100,
    application_complete_ms: int | None = 110,
    complete: bool = True,
    status_code: int | None = 200,
    segments: list[TraceSegment] | None = None,
    dependencies: list[LogicalDependencyNode] | None = None,
) -> RequestTraceSnapshot:
    trace = RequestTrace(
        schema_version="1.0",
        id="trace-1",
        method="GET",
        path="/items",
        route="/items",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_received_ns=0,
        response_started_ns=10 * NS_PER_MS
        if response_complete_ms is not None
        else None,
        response_body_completed_ns=(
            None if response_complete_ms is None else response_complete_ms * NS_PER_MS
        ),
        application_completed_ns=(
            None
            if application_complete_ms is None
            else application_complete_ms * NS_PER_MS
        ),
        status_code=status_code,
        segments=[] if segments is None else segments,
        logical_dependencies=[] if dependencies is None else dependencies,
        complete=complete,
    )
    return trace.snapshot()


def sql_segment(
    segment_id: str,
    *,
    start_ms: int,
    end_ms: int,
    fingerprint: str = "fingerprint-a",
) -> TraceSegment:
    return make_segment(
        segment_id,
        segment_type=SegmentType.SQL,
        start_ms=start_ms,
        end_ms=end_ms,
        name="SELECT query",
        attributes={"statement_fingerprint": fingerprint},
    )


def test_slow_request_uses_response_complete_duration() -> None:
    rule = SlowRequestRule(threshold_ms=100)

    findings = rule.evaluate(make_trace(response_complete_ms=100))

    assert findings == [
        Diagnostic(
            code="slow_request",
            severity="warning",
            message=(
                "Request response-complete duration was 100.00 ms, "
                "exceeding the 100.00 ms threshold."
            ),
        )
    ]
    assert rule.evaluate(make_trace(response_complete_ms=99)) == []
    assert rule.evaluate(make_trace(response_complete_ms=None)) == []


def test_slow_dependency_reports_phase_and_explicit_response_denominator() -> None:
    setup = make_segment(
        "dependency-setup",
        segment_type=SegmentType.DEPENDENCY_SETUP,
        start_ms=20,
        end_ms=80,
        name="authenticate",
    )
    cleanup = make_segment(
        "dependency-cleanup",
        segment_type=SegmentType.DEPENDENCY_CLEANUP,
        start_ms=100,
        end_ms=105,
        name="authenticate",
    )
    trace = make_trace(
        response_complete_ms=200,
        application_complete_ms=210,
        segments=[setup, cleanup],
    )
    rule = SlowDependencyRule(
        absolute_threshold_ms=100,
        percentage_threshold=25,
    )

    findings = rule.evaluate(trace)

    assert len(findings) == 1
    assert findings[0].segment_id == "dependency-setup"
    assert findings[0].message == (
        "Dependency setup 'authenticate' took 60.00 ms, exceeding "
        "25.00% of response-complete duration (200.00 ms)."
    )


def test_slow_dependency_falls_back_to_application_duration() -> None:
    segment = make_segment(
        "dependency-cleanup",
        segment_type=SegmentType.DEPENDENCY_CLEANUP,
        start_ms=100,
        end_ms=160,
        name="resource",
    )
    trace = make_trace(
        response_complete_ms=None,
        application_complete_ms=200,
        complete=False,
        status_code=None,
        segments=[segment],
    )

    finding = SlowDependencyRule(
        absolute_threshold_ms=100,
        percentage_threshold=25,
    ).evaluate(trace)[0]

    assert "Dependency cleanup 'resource'" in finding.message
    assert "25.00% of application duration (200.00 ms)" in finding.message


def test_post_response_dependency_uses_application_denominator() -> None:
    segment = make_segment(
        "dependency-cleanup",
        segment_type=SegmentType.DEPENDENCY_CLEANUP,
        start_ms=120,
        end_ms=180,
        name="resource",
    )
    trace = make_trace(
        response_complete_ms=100,
        application_complete_ms=200,
        segments=[segment],
    )

    finding = SlowDependencyRule(
        absolute_threshold_ms=100,
        percentage_threshold=25,
    ).evaluate(trace)[0]

    assert "30.00% of application duration (200.00 ms)" not in finding.message
    assert "25.00% of application duration (200.00 ms)" in finding.message


def test_repeated_query_uses_interval_union_for_overlapping_segments() -> None:
    trace = make_trace(
        segments=[
            sql_segment("sql-1", start_ms=10, end_ms=50),
            sql_segment("sql-2", start_ms=30, end_ms=70),
            sql_segment("sql-3", start_ms=80, end_ms=90),
            sql_segment(
                "other",
                start_ms=91,
                end_ms=92,
                fingerprint="fingerprint-b",
            ),
        ]
    )

    findings = RepeatedQueryRule(minimum_occurrences=2).evaluate(trace)

    assert len(findings) == 1
    assert findings[0].segment_id == "sql-1"
    assert "occurred 3 times, covering 70.00 ms of wall time" in findings[0].message
    assert "does not assert a defect" in findings[0].message


def test_repeated_query_reports_invalid_group_timing_without_summing_it() -> None:
    trace = make_trace(
        segments=[
            sql_segment("sql-1", start_ms=20, end_ms=10),
            sql_segment("sql-2", start_ms=30, end_ms=40),
        ]
    )

    finding = RepeatedQueryRule().evaluate(trace)[0]

    assert "with an invalid timing interval" in finding.message


def test_possible_n_plus_one_is_overlap_aware_and_explicitly_heuristic() -> None:
    segments = [
        sql_segment(
            f"sql-{index}",
            start_ms=index * 2,
            end_ms=index * 2 + 2,
        )
        for index in range(10)
    ]
    trace = make_trace(response_complete_ms=100, segments=segments)

    findings = PossibleNPlusOneRule(
        minimum_occurrences=10,
        minimum_percentage=10,
    ).evaluate(trace)

    assert len(findings) == 1
    assert "Possible N+1 heuristic" in findings[0].message
    assert "20.00% of response-complete duration (100.00 ms)" in findings[0].message
    assert "parameter variation is inferred rather than proven" in findings[0].message


def test_possible_n_plus_one_requires_count_duration_and_denominator() -> None:
    nine_segments = [
        sql_segment(f"sql-{index}", start_ms=index, end_ms=index + 1)
        for index in range(9)
    ]
    assert PossibleNPlusOneRule().evaluate(make_trace(segments=nine_segments)) == []

    invalid_segments = [
        sql_segment(f"sql-{index}", start_ms=20, end_ms=10) for index in range(10)
    ]
    assert PossibleNPlusOneRule().evaluate(make_trace(segments=invalid_segments)) == []

    no_denominator = make_trace(
        response_complete_ms=None,
        application_complete_ms=None,
        complete=False,
        status_code=None,
        segments=[
            sql_segment(f"sql-{index}", start_ms=index, end_ms=index + 1)
            for index in range(10)
        ],
    )
    assert PossibleNPlusOneRule().evaluate(no_denominator) == []


def test_expensive_serialization_reports_combined_segment_denominator() -> None:
    serialization = make_segment(
        "serialization",
        segment_type=SegmentType.SERIALIZATION,
        start_ms=50,
        end_ms=80,
        name="response serialization",
    )
    trace = make_trace(response_complete_ms=100, segments=[serialization])

    finding = ExpensiveSerializationRule(
        absolute_threshold_ms=50,
        percentage_threshold=20,
    ).evaluate(trace)[0]

    assert finding.segment_id == "serialization"
    assert finding.message == (
        "Combined response serialization took 30.00 ms, exceeding "
        "20.00% of response-complete duration (100.00 ms)."
    )


def test_integrity_rule_reports_incomplete_and_invalid_segments() -> None:
    segments = [
        make_segment(
            "foreign",
            segment_type=SegmentType.CUSTOM,
            start_ms=5,
            end_ms=10,
            trace_id="other-trace",
            parent_id="missing-parent",
        ),
        make_segment(
            "incomplete",
            segment_type=SegmentType.CUSTOM,
            start_ms=20,
            end_ms=None,
            status=SegmentStatus.INCOMPLETE,
        ),
        make_segment(
            "negative",
            segment_type=SegmentType.CUSTOM,
            start_ms=40,
            end_ms=30,
        ),
        make_segment(
            "outside",
            segment_type=SegmentType.CUSTOM,
            start_ms=100,
            end_ms=120,
        ),
    ]
    trace = make_trace(
        response_complete_ms=None,
        application_complete_ms=110,
        complete=False,
        status_code=None,
        segments=segments,
    )

    findings = IntegrityRule().evaluate(trace)
    messages = {finding.message for finding in findings}

    assert "Trace is incomplete; missing or invalid: response body completion." in (
        messages
    )
    assert "Segment trace ID does not match its request trace." in messages
    assert "Segment parent does not exist in this trace." in messages
    assert "Segment has no complete timing interval." in messages
    assert "Segment has a negative duration." in messages
    assert "Segment falls outside the request lifecycle." in messages


def test_integrity_rule_reports_lifecycle_and_dependency_references() -> None:
    dependency = LogicalDependencyNode(
        id="dependency-1",
        trace_id="trace-1",
        name="dependency",
        cache_status=DependencyCacheStatus.MISS,
        parent_id="missing-dependency",
        setup_segment_id="missing-setup",
    )
    trace = RequestTrace(
        schema_version="1.0",
        id="trace-1",
        method="GET",
        path="/items",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_received_ns=0,
        response_started_ns=90 * NS_PER_MS,
        response_body_completed_ns=80 * NS_PER_MS,
        application_completed_ns=70 * NS_PER_MS,
        status_code=None,
        logical_dependencies=[dependency],
        complete=True,
    ).snapshot()

    messages = {finding.message for finding in IntegrityRule().evaluate(trace)}

    assert "Response-start checkpoint has no HTTP status code." in messages
    assert (
        "Lifecycle checkpoint 'response body completion' occurs before "
        "'response start'."
    ) in messages
    assert (
        "Lifecycle checkpoint 'application completion' occurs before "
        "'response body completion'."
    ) in messages
    assert (
        "Logical dependency parent reference does not exist: dependency-1."
    ) in messages
    assert (
        "Logical dependency setup segment reference does not exist: dependency-1."
    ) in messages


def test_integrity_rule_reports_body_without_response_start() -> None:
    trace = RequestTrace(
        schema_version="1.0",
        id="trace-1",
        method="GET",
        path="/items",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_received_ns=0,
        response_started_ns=None,
        response_body_completed_ns=50 * NS_PER_MS,
        application_completed_ns=60 * NS_PER_MS,
        status_code=None,
        complete=True,
    ).snapshot()

    finding = IntegrityRule().evaluate(trace)[0]

    assert finding.message == (
        "Response body completed without a response-start checkpoint."
    )


class FailingRule:
    code = "failing"

    def evaluate(self, _trace: RequestTraceSnapshot) -> list[Diagnostic]:
        raise RuntimeError("expected diagnostic failure")


class FindingRule:
    code = "finding"

    def evaluate(self, _trace: RequestTraceSnapshot) -> list[Diagnostic]:
        return [
            Diagnostic(
                code=self.code,
                severity="info",
                message="Finding survived.",
            )
        ]


def test_diagnostic_engine_isolates_rule_failures() -> None:
    engine = DiagnosticEngine(rules=(FailingRule(), FindingRule()))

    findings = engine.evaluate(make_trace())

    assert findings == [
        Diagnostic(
            code="diagnostic_rule_failure",
            severity="error",
            message="Diagnostic rule 'failing' failed.",
        ),
        Diagnostic(
            code="finding",
            severity="info",
            message="Finding survived.",
        ),
    ]


def test_diagnostic_engine_builds_configured_default_rules() -> None:
    engine = DiagnosticEngine(
        config=DiagnosticConfig(
            slow_request_threshold_ms=50,
            slow_dependency_threshold_ms=1_000,
            slow_dependency_percentage=100,
            repeated_query_minimum=20,
            n_plus_one_minimum=20,
            n_plus_one_percentage=100,
            serialization_threshold_ms=1_000,
            serialization_percentage=100,
        )
    )

    findings = engine.evaluate(make_trace(response_complete_ms=60))

    assert [finding.code for finding in findings] == ["slow_request"]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SlowRequestRule(-1),
        lambda: SlowDependencyRule(-1, 10),
        lambda: SlowDependencyRule(10, -1),
        lambda: RepeatedQueryRule(1),
        lambda: PossibleNPlusOneRule(1, 10),
        lambda: PossibleNPlusOneRule(10, -1),
        lambda: ExpensiveSerializationRule(-1, 10),
        lambda: ExpensiveSerializationRule(10, -1),
    ],
)
def test_invalid_rule_thresholds_fail_fast(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]

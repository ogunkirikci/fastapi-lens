"""Built-in trace diagnostic rules."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from typing import TypeAlias

from fastapi_latensight.models import (
    Diagnostic,
    FrozenJsonValue,
    RequestTraceSnapshot,
    SegmentStatus,
    SegmentType,
    TraceSegmentSnapshot,
)
from fastapi_latensight.utils.timing import (
    InvalidIntervalError,
    interval_union_duration_ns,
)

_DurationDenominator: TypeAlias = tuple[float, str]


def _require_non_negative(value: float, *, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be zero or greater.")


def _segment_attribute(
    segment: TraceSegmentSnapshot,
    name: str,
) -> FrozenJsonValue | None:
    return dict(segment.attributes).get(name)


def _duration_denominator(
    trace: RequestTraceSnapshot,
    *,
    allow_application_fallback: bool,
    observed_end_ns: int | None = None,
) -> _DurationDenominator | None:
    application_duration = trace.application_duration_ms
    if (
        allow_application_fallback
        and observed_end_ns is not None
        and trace.response_body_completed_ns is not None
        and observed_end_ns > trace.response_body_completed_ns
        and application_duration is not None
        and application_duration > 0
    ):
        return application_duration, "application duration"
    response_duration = trace.response_complete_duration_ms
    if response_duration is not None and response_duration > 0:
        return response_duration, "response-complete duration"
    if (
        allow_application_fallback
        and application_duration is not None
        and application_duration > 0
    ):
        return application_duration, "application duration"
    return None


def _union_duration_ms(
    segments: Iterable[TraceSegmentSnapshot],
    *,
    bounds: tuple[int, int] | None = None,
) -> float | None:
    intervals = []
    for segment in segments:
        if segment.end_ns is None:
            return None
        intervals.append((segment.start_ns, segment.end_ns))
    try:
        return interval_union_duration_ns(intervals, bounds=bounds) / 1_000_000
    except InvalidIntervalError:
        return None


def _threshold_reasons(
    *,
    duration_ms: float,
    absolute_threshold_ms: float,
    percentage_threshold: float,
    denominator: _DurationDenominator | None,
) -> list[str]:
    reasons: list[str] = []
    if duration_ms >= absolute_threshold_ms:
        reasons.append(f"{absolute_threshold_ms:.2f} ms absolute threshold")
    if denominator is not None:
        denominator_ms, denominator_name = denominator
        percentage = duration_ms / denominator_ms * 100
        if percentage >= percentage_threshold:
            reasons.append(
                f"{percentage_threshold:.2f}% of {denominator_name} "
                f"({denominator_ms:.2f} ms)"
            )
    return reasons


@dataclass(slots=True, frozen=True)
class SlowRequestRule:
    """Report requests exceeding an absolute response-complete threshold."""

    threshold_ms: float = 250.0
    code: str = "slow_request"

    def __post_init__(self) -> None:
        _require_non_negative(self.threshold_ms, name="threshold_ms")

    def evaluate(self, trace: RequestTraceSnapshot) -> list[Diagnostic]:
        duration_ms = trace.response_complete_duration_ms
        if duration_ms is None or duration_ms < self.threshold_ms:
            return []
        return [
            Diagnostic(
                code=self.code,
                severity="warning",
                message=(
                    f"Request response-complete duration was {duration_ms:.2f} ms, "
                    f"exceeding the {self.threshold_ms:.2f} ms threshold."
                ),
            )
        ]


@dataclass(slots=True, frozen=True)
class SlowDependencyRule:
    """Report slow dependency setup or cleanup segments."""

    absolute_threshold_ms: float = 100.0
    percentage_threshold: float = 25.0
    code: str = "slow_dependency"

    def __post_init__(self) -> None:
        _require_non_negative(
            self.absolute_threshold_ms,
            name="absolute_threshold_ms",
        )
        _require_non_negative(
            self.percentage_threshold,
            name="percentage_threshold",
        )

    def evaluate(self, trace: RequestTraceSnapshot) -> list[Diagnostic]:
        findings: list[Diagnostic] = []
        dependency_types = {
            SegmentType.DEPENDENCY_SETUP,
            SegmentType.DEPENDENCY_CLEANUP,
        }
        for segment in trace.segments:
            if segment.type not in dependency_types:
                continue
            duration_ms = segment.duration_ms
            if duration_ms is None:
                continue
            denominator = _duration_denominator(
                trace,
                allow_application_fallback=True,
                observed_end_ns=segment.end_ns,
            )
            reasons = _threshold_reasons(
                duration_ms=duration_ms,
                absolute_threshold_ms=self.absolute_threshold_ms,
                percentage_threshold=self.percentage_threshold,
                denominator=denominator,
            )
            if not reasons:
                continue
            phase = (
                "setup" if segment.type is SegmentType.DEPENDENCY_SETUP else "cleanup"
            )
            findings.append(
                Diagnostic(
                    code=self.code,
                    severity="warning",
                    message=(
                        f"Dependency {phase} '{segment.name}' took "
                        f"{duration_ms:.2f} ms, exceeding "
                        f"{' and '.join(reasons)}."
                    ),
                    segment_id=segment.id,
                )
            )
        return findings


@dataclass(slots=True, frozen=True)
class RepeatedQueryRule:
    """Report SQL fingerprints repeated within one request."""

    minimum_occurrences: int = 2
    code: str = "repeated_query"

    def __post_init__(self) -> None:
        if self.minimum_occurrences < 2:
            raise ValueError("minimum_occurrences must be at least two.")

    def evaluate(self, trace: RequestTraceSnapshot) -> list[Diagnostic]:
        groups = _sql_groups(trace)
        findings: list[Diagnostic] = []
        bounds = (
            (trace.request_received_ns, trace.application_completed_ns)
            if trace.application_completed_ns is not None
            else None
        )
        for fingerprint, segments in sorted(groups.items()):
            if len(segments) < self.minimum_occurrences:
                continue
            union_duration_ms = _union_duration_ms(segments, bounds=bounds)
            duration_text = (
                "with an invalid timing interval"
                if union_duration_ms is None
                else f"covering {union_duration_ms:.2f} ms of wall time"
            )
            findings.append(
                Diagnostic(
                    code=self.code,
                    severity="info",
                    message=(
                        f"SQL fingerprint {fingerprint} occurred "
                        f"{len(segments)} times, {duration_text}. "
                        "This reports repetition and does not assert a defect."
                    ),
                    segment_id=segments[0].id,
                )
            )
        return findings


@dataclass(slots=True, frozen=True)
class PossibleNPlusOneRule:
    """Report a conservative, explicitly heuristic N+1 signal."""

    minimum_occurrences: int = 10
    minimum_percentage: float = 10.0
    code: str = "possible_n_plus_one"

    def __post_init__(self) -> None:
        if self.minimum_occurrences < 2:
            raise ValueError("minimum_occurrences must be at least two.")
        _require_non_negative(
            self.minimum_percentage,
            name="minimum_percentage",
        )

    def evaluate(self, trace: RequestTraceSnapshot) -> list[Diagnostic]:
        findings: list[Diagnostic] = []
        for fingerprint, segments in sorted(_sql_groups(trace).items()):
            if len(segments) < self.minimum_occurrences:
                continue
            union_duration_ms = _union_duration_ms(segments)
            if union_duration_ms is None:
                continue
            observed_end_ns = max(
                segment.end_ns for segment in segments if segment.end_ns is not None
            )
            denominator = _duration_denominator(
                trace,
                allow_application_fallback=True,
                observed_end_ns=observed_end_ns,
            )
            if denominator is None:
                continue
            denominator_ms, denominator_name = denominator
            percentage = union_duration_ms / denominator_ms * 100
            if percentage < self.minimum_percentage:
                continue
            findings.append(
                Diagnostic(
                    code=self.code,
                    severity="warning",
                    message=(
                        f"Possible N+1 heuristic: SQL fingerprint {fingerprint} "
                        f"occurred {len(segments)} times and covered "
                        f"{percentage:.2f}% of {denominator_name} "
                        f"({denominator_ms:.2f} ms). Bind values are not stored, "
                        "so parameter variation is inferred rather than proven."
                    ),
                    segment_id=segments[0].id,
                )
            )
        return findings


@dataclass(slots=True, frozen=True)
class ExpensiveSerializationRule:
    """Report expensive combined response serialization segments."""

    absolute_threshold_ms: float = 50.0
    percentage_threshold: float = 20.0
    code: str = "expensive_serialization"

    def __post_init__(self) -> None:
        _require_non_negative(
            self.absolute_threshold_ms,
            name="absolute_threshold_ms",
        )
        _require_non_negative(
            self.percentage_threshold,
            name="percentage_threshold",
        )

    def evaluate(self, trace: RequestTraceSnapshot) -> list[Diagnostic]:
        denominator = _duration_denominator(
            trace,
            allow_application_fallback=False,
        )
        findings: list[Diagnostic] = []
        for segment in trace.segments:
            if segment.type is not SegmentType.SERIALIZATION:
                continue
            duration_ms = segment.duration_ms
            if duration_ms is None:
                continue
            reasons = _threshold_reasons(
                duration_ms=duration_ms,
                absolute_threshold_ms=self.absolute_threshold_ms,
                percentage_threshold=self.percentage_threshold,
                denominator=denominator,
            )
            if reasons:
                findings.append(
                    Diagnostic(
                        code=self.code,
                        severity="warning",
                        message=(
                            f"Combined response serialization took "
                            f"{duration_ms:.2f} ms, exceeding "
                            f"{' and '.join(reasons)}."
                        ),
                        segment_id=segment.id,
                    )
                )
        return findings


@dataclass(slots=True, frozen=True)
class IntegrityRule:
    """Report incomplete lifecycle state and invalid trace relationships."""

    code: str = "trace_integrity"

    def evaluate(self, trace: RequestTraceSnapshot) -> list[Diagnostic]:
        findings: list[Diagnostic] = []
        if not trace.complete:
            missing = []
            if trace.response_body_completed_ns is None:
                missing.append("response body completion")
            if trace.application_completed_ns is None:
                missing.append("application completion")
            detail = ", ".join(missing) if missing else "collector integrity"
            findings.append(
                Diagnostic(
                    code="incomplete_trace",
                    severity="warning",
                    message=f"Trace is incomplete; missing or invalid: {detail}.",
                )
            )

        if (
            trace.response_body_completed_ns is not None
            and trace.response_started_ns is None
        ):
            findings.append(
                self._trace_finding(
                    "Response body completed without a response-start checkpoint."
                )
            )
        if trace.response_started_ns is not None and trace.status_code is None:
            findings.append(
                self._trace_finding(
                    "Response-start checkpoint has no HTTP status code."
                )
            )
        checkpoints = (
            ("request receipt", trace.request_received_ns),
            ("response start", trace.response_started_ns),
            ("response body completion", trace.response_body_completed_ns),
            ("application completion", trace.application_completed_ns),
        )
        observed_checkpoints = [
            (name, timestamp)
            for name, timestamp in checkpoints
            if timestamp is not None
        ]
        for (previous_name, previous_ns), (current_name, current_ns) in pairwise(
            observed_checkpoints
        ):
            if current_ns < previous_ns:
                findings.append(
                    self._trace_finding(
                        f"Lifecycle checkpoint '{current_name}' occurs before "
                        f"'{previous_name}'."
                    )
                )

        lifecycle_end = trace.application_completed_ns
        lifecycle_bounds = (
            (trace.request_received_ns, lifecycle_end)
            if lifecycle_end is not None
            else None
        )
        segment_ids = {segment.id for segment in trace.segments}
        dependency_ids = {dependency.id for dependency in trace.logical_dependencies}
        for segment in trace.segments:
            if segment.trace_id != trace.id:
                findings.append(
                    self._finding(
                        "Segment trace ID does not match its request trace.",
                        segment,
                    )
                )
            if segment.parent_id is not None and segment.parent_id not in segment_ids:
                findings.append(
                    self._finding(
                        "Segment parent does not exist in this trace.",
                        segment,
                    )
                )
            if (
                segment.logical_dependency_id is not None
                and segment.logical_dependency_id not in dependency_ids
            ):
                findings.append(
                    self._finding(
                        "Segment logical dependency does not exist in this trace.",
                        segment,
                    )
                )
            if segment.end_ns is None or segment.status is SegmentStatus.INCOMPLETE:
                findings.append(
                    self._finding("Segment has no complete timing interval.", segment)
                )
                continue
            if segment.end_ns < segment.start_ns:
                findings.append(
                    self._finding("Segment has a negative duration.", segment)
                )
                continue
            if lifecycle_bounds is not None and (
                segment.start_ns < lifecycle_bounds[0]
                or segment.end_ns > lifecycle_bounds[1]
            ):
                findings.append(
                    self._finding(
                        "Segment falls outside the request lifecycle.",
                        segment,
                    )
                )

        for dependency in trace.logical_dependencies:
            if dependency.trace_id != trace.id:
                findings.append(
                    self._trace_finding(
                        "Logical dependency trace ID does not match its request trace: "
                        f"{dependency.id}."
                    )
                )
            references = (
                ("parent", dependency.parent_id, dependency_ids),
                ("cache source", dependency.cached_from_id, dependency_ids),
                ("setup segment", dependency.setup_segment_id, segment_ids),
                ("cleanup segment", dependency.cleanup_segment_id, segment_ids),
            )
            for reference_name, reference_id, valid_ids in references:
                if reference_id is not None and reference_id not in valid_ids:
                    findings.append(
                        self._trace_finding(
                            f"Logical dependency {reference_name} reference "
                            f"does not exist: {dependency.id}."
                        )
                    )
        return findings

    def _finding(
        self,
        message: str,
        segment: TraceSegmentSnapshot,
    ) -> Diagnostic:
        return Diagnostic(
            code=self.code,
            severity="error",
            message=message,
            segment_id=segment.id,
        )

    def _trace_finding(self, message: str) -> Diagnostic:
        return Diagnostic(
            code=self.code,
            severity="error",
            message=message,
        )


def _sql_groups(
    trace: RequestTraceSnapshot,
) -> dict[str, list[TraceSegmentSnapshot]]:
    groups: dict[str, list[TraceSegmentSnapshot]] = defaultdict(list)
    for segment in trace.segments:
        if segment.type is not SegmentType.SQL:
            continue
        fingerprint = _segment_attribute(segment, "statement_fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            groups[fingerprint].append(segment)
    return dict(groups)

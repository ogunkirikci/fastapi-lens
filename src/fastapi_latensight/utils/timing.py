"""Validated interval calculations for trace timelines."""

from collections.abc import Iterable
from enum import StrEnum
from typing import TypeAlias

Interval: TypeAlias = tuple[int, int]


class IntervalErrorReason(StrEnum):
    """The reason an interval cannot participate in timeline calculations."""

    NEGATIVE_DURATION = "negative_duration"
    OUT_OF_BOUNDS = "out_of_bounds"


class InvalidIntervalError(ValueError):
    """Raised when timeline mathematics encounters an invalid interval."""

    def __init__(
        self,
        interval: Interval,
        reason: IntervalErrorReason,
        *,
        bounds: Interval | None = None,
    ) -> None:
        self.interval = interval
        self.reason = reason
        self.bounds = bounds
        if bounds is None:
            message = f"Invalid interval {interval}: {reason.value}."
        else:
            message = (
                f"Invalid interval {interval}: {reason.value} for bounds {bounds}."
            )
        super().__init__(message)


def _validate_interval(
    interval: Interval,
    *,
    bounds: Interval | None = None,
) -> None:
    start_ns, end_ns = interval
    if end_ns < start_ns:
        raise InvalidIntervalError(
            interval,
            IntervalErrorReason.NEGATIVE_DURATION,
        )
    if bounds is None:
        return
    bounds_start_ns, bounds_end_ns = bounds
    if start_ns < bounds_start_ns or end_ns > bounds_end_ns:
        raise InvalidIntervalError(
            interval,
            IntervalErrorReason.OUT_OF_BOUNDS,
            bounds=bounds,
        )


def merge_intervals_ns(
    intervals: Iterable[Interval],
    *,
    bounds: Interval | None = None,
) -> tuple[Interval, ...]:
    """Return sorted non-overlapping intervals after strict validation."""
    if bounds is not None:
        _validate_interval(bounds)

    ordered = sorted(intervals)
    merged: list[Interval] = []
    for interval in ordered:
        _validate_interval(interval, bounds=bounds)
        start_ns, end_ns = interval
        if not merged or start_ns > merged[-1][1]:
            merged.append(interval)
            continue
        previous_start_ns, previous_end_ns = merged[-1]
        merged[-1] = (previous_start_ns, max(previous_end_ns, end_ns))
    return tuple(merged)


def interval_union_duration_ns(
    intervals: Iterable[Interval],
    *,
    bounds: Interval | None = None,
) -> int:
    """Return the duration of the interval union without double counting."""
    return sum(
        end_ns - start_ns
        for start_ns, end_ns in merge_intervals_ns(intervals, bounds=bounds)
    )


def self_duration_ns(
    parent: Interval,
    child_intervals: Iterable[Interval],
) -> int:
    """Return parent duration excluding the union of valid child intervals."""
    _validate_interval(parent)
    parent_start_ns, parent_end_ns = parent
    child_duration_ns = interval_union_duration_ns(
        child_intervals,
        bounds=parent,
    )
    return parent_end_ns - parent_start_ns - child_duration_ns


def unattributed_duration_ns(
    lifecycle: Interval,
    measured_intervals: Iterable[Interval],
) -> int:
    """Return lifecycle duration not covered by measured top-level intervals."""
    return self_duration_ns(lifecycle, measured_intervals)
